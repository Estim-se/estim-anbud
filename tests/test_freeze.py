"""The freeze ledger: chained lines, hand-made records imported byte for byte, frozen outputs read-only, and the
`anbud run` hook. Every run and record here is synthetic."""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import assert_no_fake_names

from anbud import cli
from anbud.evals import freeze
from anbud.evals.freeze import FreezeError, freeze_run, import_manual, is_frozen, ledger_file, verify
from anbud.granskning import command

CODE = {"harness_sha": "a" * 40, "dirty": False, "diff_sha256": None}
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_run(data_root: Path, run_id: str, tender: str = "t-0001", company: str = "co-001") -> tuple[Path, Path]:
    """A finished Granskning run's two folders, with invented content."""
    outputs = data_root / company / "dev" / tender / "runs" / run_id
    (outputs / "report").mkdir(parents=True)
    (outputs / "flags.json").write_text(f'[{{"claim": "syntetisk {run_id}"}}]\n')
    (outputs / "report" / "granskning.html").write_text("<p>syntetisk</p>\n")
    record = data_root / company / "runs" / run_id
    record.mkdir(parents=True)
    (record / "run.json").write_text(json.dumps({"salt": "s", "company_id": company, "code": CODE}))
    (record / "events.jsonl").write_text('{"kind":"start"}\n')
    return outputs, record


def _freeze(data_root: Path, run_id: str, tender: str = "t-0001") -> dict[str, Any]:
    outputs, record = make_run(data_root, run_id, tender)
    return freeze_run("co-001", run_id, outputs_dir=outputs, run_record_dir=record, tender_id=tender, now=NOW)


def manual_file(folder: Path) -> Path:
    """Two hand-made, chained records, deliberately not in canonical form, so byte identity is tested."""
    first = json.dumps({"v": 0, "kind": "manual_freeze_v0", "tender": "co-001/t-0001", "run_id": "hand-1"}).encode()
    second = json.dumps(
        {"kind": "manual_freeze_v0", "tender": "co-001/t-0002", "run_id": "hand-2", "prev": _sha(first)}, indent=None
    ).encode()
    path = folder / "manual.jsonl"
    path.write_bytes(first + b"\n" + second + b"\n")
    return path


def test_three_freezes_chain_and_verify(data_root: Path) -> None:
    records = [_freeze(data_root, f"r{i}", tender=f"t-000{i}") for i in (1, 2, 3)]
    ledger = ledger_file("co-001")
    assert ledger == data_root / "predictions" / "co-001" / "ledger.jsonl"
    lines = ledger.read_bytes().splitlines()
    assert [r["line_number"] for r in records] == [1, 2, 3]
    assert "prev" not in json.loads(lines[0])
    assert json.loads(lines[1])["prev"] == _sha(lines[0]) and json.loads(lines[2])["prev"] == _sha(lines[1])
    assert records[2]["line_sha256"] == _sha(lines[2])
    summary = verify(ledger)
    assert (summary.lines, summary.last_sha256) == (3, _sha(lines[2]))
    assert summary.kinds == {"freeze": 3} and summary.tenders == ("co-001/t-0001", "co-001/t-0002", "co-001/t-0003")
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o600


def test_the_record_holds_hashes_and_relative_paths(data_root: Path) -> None:
    tender = data_root / "co-001" / "dev" / "t-0001"
    (tender / "prep").mkdir(parents=True)
    (tender / "prep" / "blocks.jsonl").write_text("{}\n")
    (tender / "inbox.sha256").write_text("0" * 64 + "  a\n")
    record = _freeze(data_root, "r1")
    assert record["tender"] == "co-001/t-0001" and record["harness"] == CODE
    assert record["outputs_dir"] == "co-001/dev/t-0001/runs/r1" and record["run_record_dir"] == "co-001/runs/r1"
    assert set(record["outputs_sha256"]) == {"flags.json", "report/granskning.html"}
    assert set(record["run_record_sha256"]) == {"events.jsonl", "run.json"}
    assert record["prep_sha256"] == {"blocks.jsonl": _sha(b"{}\n")}
    assert record["input_manifest_sha256"] == _sha((tender / "inbox.sha256").read_bytes())
    assert "config/providers.toml" in record["config_sha256"] and "schemas/tools.json" in record["config_sha256"]
    assert any(p.startswith("lenses/") for p in record["config_sha256"])
    assert record["frozen_at"] == "2026-11-26T12:00:00+00:00"


@pytest.mark.parametrize("line", [0, 1])
def test_editing_an_earlier_line_breaks_the_chain(data_root: Path, line: int) -> None:
    for i in (1, 2, 3):
        _freeze(data_root, f"r{i}")
    ledger = ledger_file("co-001")
    lines = ledger.read_bytes().splitlines(keepends=True)
    lines[line] = lines[line].replace(b'"v":1', b'"v":2')
    ledger.write_bytes(b"".join(lines))
    with pytest.raises(FreezeError, match=f"ledger line {line + 2}: prev"):
        verify(ledger)
    with pytest.raises(FreezeError):
        _freeze(data_root, "r4")  # nothing is appended to a broken chain


def test_a_cut_off_last_line_and_other_damage_are_refused(data_root: Path) -> None:
    _freeze(data_root, "r1")
    ledger = ledger_file("co-001")
    good = ledger.read_bytes()
    for damaged, reason in [
        (good[:-1], "no trailing newline"),
        (good + b"\n", "blank"),
        (good + b"[1]\n", "not a JSON object"),
        (good + b"{nope\n", "not JSON"),
        (b'{"prev":"x"}\n', "first line has a prev"),
    ]:
        ledger.write_bytes(damaged)
        with pytest.raises(FreezeError, match=reason) as caught:
            verify(ledger)
        assert "syntetisk" not in str(caught.value)


def test_a_symlinked_ledger_is_refused_even_dangling(data_root: Path, tmp_path: Path) -> None:
    folder = data_root / "predictions" / "co-001"
    folder.mkdir(parents=True)
    (folder / "ledger.jsonl").symlink_to(tmp_path / "nowhere.jsonl")
    with pytest.raises(FreezeError, match="symlink"):
        _freeze(data_root, "r1")
    assert not (tmp_path / "nowhere.jsonl").exists()


@pytest.mark.parametrize("sealed", [False, True])
def test_a_symlinked_folder_above_the_ledger_is_refused_when_reading(
    data_root: Path, tmp_path: Path, sealed: bool
) -> None:
    """A redirected company folder could point the freeze check at a forged chain that unlocks the answer store."""
    forged = tmp_path / "forged"
    forged.mkdir()
    (forged / "ledger.jsonl").write_text('{"kind":"freeze","tender":"co-001/t-0001","run_id":"r1"}\n')
    link = ledger_file("co-001", sealed=sealed).parent
    link.parent.mkdir(parents=True)
    link.symlink_to(forged, target_is_directory=True)
    with pytest.raises(FreezeError, match="symlink"):
        is_frozen("co-001", "t-0001", sealed=sealed)
    with pytest.raises(FreezeError, match="symlink"):
        verify(ledger_file("co-001", sealed=sealed))
    with pytest.raises(FreezeError, match="symlink"):
        freeze.frozen_line("co-001", "r1", sealed=sealed)


def test_a_symlinked_ledger_folder_is_refused(data_root: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (data_root / "predictions").mkdir()
    (data_root / "predictions" / "co-001").symlink_to(elsewhere)
    with pytest.raises(FreezeError, match="symlink"):
        _freeze(data_root, "r1")


def test_hand_made_records_start_the_ledger_byte_for_byte(data_root: Path, tmp_path: Path) -> None:
    source = manual_file(tmp_path)
    assert import_manual("co-001", source) == 2
    ledger = ledger_file("co-001")
    assert ledger.read_bytes() == source.read_bytes()
    assert import_manual("co-001", source) == 0  # again: a no-op
    record = _freeze(data_root, "r1", tender="t-0003")
    lines = ledger.read_bytes().splitlines()
    assert ledger.read_bytes().startswith(source.read_bytes())
    assert record["line_number"] == 3 and json.loads(lines[2])["prev"] == _sha(lines[1])
    assert verify(ledger).kinds == {"freeze": 1, "manual_freeze_v0": 2}
    assert is_frozen("co-001", "t-0002") and is_frozen("co-001", "t-0003") and not is_frozen("co-001", "t-0004")
    assert import_manual("co-001", source) == 0


def test_hand_made_records_refused_after_other_lines_or_with_a_broken_chain(data_root: Path, tmp_path: Path) -> None:
    source = manual_file(tmp_path)
    _freeze(data_root, "r1")
    with pytest.raises(FreezeError, match="only start a ledger"):
        import_manual("co-001", source)
    broken = tmp_path / "broken.jsonl"
    lines = source.read_bytes().splitlines(keepends=True)
    broken.write_bytes(lines[0].replace(b"hand-1", b"hand-9") + lines[1])
    with pytest.raises(FreezeError, match="hand-made records: ledger line 2"):
        import_manual("co-002", broken)
    assert not ledger_file("co-002").exists()


def test_outputs_outside_the_company_folder_or_in_sealed_storage_are_refused(data_root: Path, tmp_path: Path) -> None:
    _, record = make_run(data_root, "r1")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "flags.json").write_text("[]\n")
    with pytest.raises(FreezeError, match="outside the company"):
        freeze_run("co-001", "r1", outputs_dir=outside, run_record_dir=record, tender_id="t-0001")
    other = data_root / "co-002" / "x"
    other.mkdir(parents=True)
    (other / "a.json").write_text("[]\n")
    with pytest.raises(FreezeError, match="outside the company"):
        freeze_run("co-001", "r1", outputs_dir=other, run_record_dir=record, tender_id="t-0001")
    sealed = data_root / "sealed" / "co-001"
    sealed.mkdir(parents=True)
    (sealed / "a.json").write_text("[]\n")
    (data_root / "co-001" / "linked").symlink_to(sealed)
    with pytest.raises(FreezeError, match="symlink"):
        freeze_run("co-001", "r1", outputs_dir=data_root / "co-001" / "linked", run_record_dir=record, tender_id=None)
    with pytest.raises(FreezeError, match="held-out or sealed"):
        freeze_run("co-001", "r1", outputs_dir=sealed, run_record_dir=record, tender_id=None)
    assert not ledger_file("co-001").exists()


def test_empty_outputs_and_symlinked_files_are_refused(data_root: Path, tmp_path: Path) -> None:
    outputs, record = make_run(data_root, "r1")
    empty = data_root / "co-001" / "empty"
    empty.mkdir()
    with pytest.raises(FreezeError, match="no files"):
        freeze_run("co-001", "r1", outputs_dir=empty, run_record_dir=record, tender_id=None)
    (tmp_path / "target").write_text("x")
    (outputs / "link.json").symlink_to(tmp_path / "target")
    with pytest.raises(FreezeError, match="symlink"):
        freeze_run("co-001", "r1", outputs_dir=outputs, run_record_dir=record, tender_id="t-0001")


def test_outputs_become_read_only_and_a_second_freeze_is_a_no_op_or_refused(data_root: Path) -> None:
    outputs, record = make_run(data_root, "r1")
    first = freeze_run("co-001", "r1", outputs_dir=outputs, run_record_dir=record, tender_id="t-0001", now=NOW)
    for path in (outputs / "flags.json", outputs / "report" / "granskning.html"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o400
    later = datetime(2026, 11, 27, tzinfo=UTC)
    again = freeze_run("co-001", "r1", outputs_dir=outputs, run_record_dir=record, tender_id="t-0001", now=later)
    assert again["line_number"] == 1 and again["line_sha256"] == first["line_sha256"]
    assert verify(ledger_file("co-001")).lines == 1
    (outputs / "extra.json").write_text("[]\n")
    with pytest.raises(FreezeError, match="already frozen at ledger line 1"):
        freeze_run("co-001", "r1", outputs_dir=outputs, run_record_dir=record, tender_id="t-0001")


def test_a_sealed_freeze_uses_the_sealed_ledger(data_root: Path) -> None:
    outputs, record = make_run(data_root, "r1")
    freeze_run("co-001", "r1", outputs_dir=outputs, run_record_dir=record, tender_id="t-0001", sealed=True)
    assert ledger_file("co-001", sealed=True) == data_root / "sealed" / "predictions" / "co-001" / "ledger.jsonl"
    assert is_frozen("co-001", "t-0001", sealed=True) and not is_frozen("co-001", "t-0001")


def test_cli_freeze_run_verify_and_import(data_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = manual_file(tmp_path)
    assert cli.main(["freeze", "import", "co-001", str(source)]) == 0
    make_run(data_root, "r1", tender="t-0003")
    assert cli.main(["freeze", "run", "co-001", "r1"]) == 0
    out = capsys.readouterr().out
    assert "imported: 2 lines" in out and "frozen: ledger line 3, " in out
    assert cli.main(["freeze", "verify", "co-001"]) == 0
    out = capsys.readouterr().out
    assert "3 lines" in out and "freeze 1, manual_freeze_v0 2" in out and "3 tenders frozen" in out

    make_run(data_root, "r2", tender="t-0004")
    (data_root / "co-001" / "dev" / "t-0005" / "runs" / "r2").mkdir(parents=True)
    assert cli.main(["freeze", "run", "co-001", "r2"]) == cli.REFUSED  # two tenders have this run id
    assert "2 dev tenders" in capsys.readouterr().err
    assert cli.main(["freeze", "run", "co-001", "r2", "--tender", "t-0004"]) == 0
    assert cli.main(["freeze", "run", "co-001", "nope"]) == cli.REFUSED
    assert cli.main(["freeze", "verify", "Kundnamn"]) == cli.REFUSED


def test_cli_freeze_output_names_no_file(data_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    outputs, _ = make_run(data_root, "r1")
    (outputs / "Fiktivbolaget AB anbud.json").write_text("[]\n")
    kalkyl = data_root / "co-001" / "kalkyl" / "Påhittade Fastigheter"
    kalkyl.mkdir(parents=True)
    (kalkyl / "Örjan Exempelsson.json").write_text("[]\n")
    assert cli.main(["freeze", "run", "co-001", "r1"]) == 0
    assert cli.main(["freeze", "run", "co-001", "r1", "--outputs", str(kalkyl)]) == cli.REFUSED  # hashes differ
    missing = tmp_path / "Fiktivbolaget AB.jsonl"
    assert cli.main(["freeze", "import", "co-001", str(missing)]) == cli.REFUSED
    captured = capsys.readouterr()
    assert_no_fake_names(captured.out, captured.err)


# The `anbud run` hook


def _args(**kw: Any) -> argparse.Namespace:
    base = {"company_id": "co-001", "tender_id": "t-0001", "replay": False, "dry_run": False}
    return argparse.Namespace(**{**base, **kw})


@pytest.mark.parametrize(
    ("kind", "flags", "called"),
    [
        ("dev", {}, True),
        ("dev", {"replay": True}, False),  # an unfinished run replayed in place
        ("dev", {"replay": True, "base_run": "r0"}, True),  # a rebuild under its own run id
        ("dev", {"dry_run": True}, False),
        ("fixture", {}, False),
    ],
)
def test_the_run_hook_freezes_only_paid_dev_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], kind: str, flags: dict[str, bool], called: bool
) -> None:
    calls: list[tuple[str, str]] = []

    def fake(company_id: str, run_id: str, **kw: Any) -> dict[str, Any]:
        calls.append((company_id, run_id))
        return {"line_number": 7, "line_sha256": "ab" * 32}

    monkeypatch.setattr(freeze, "freeze_run", fake)
    assert command.freeze_after_run(_args(**flags), kind, "r1", Path("out"), Path("rec")) == 0
    assert bool(calls) is called
    if called:
        assert "frozen: ledger line 7, abababababab" in capsys.readouterr().out


def test_a_run_that_cannot_be_frozen_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*_: Any, **__: Any) -> dict[str, Any]:
        raise FreezeError("the outputs folder has no files")

    monkeypatch.setattr(freeze, "freeze_run", refuse)
    assert command.freeze_after_run(_args(), "dev", "r1", Path("out"), Path("rec")) == 1
    assert "not frozen: the outputs folder has no files" in capsys.readouterr().err

    def crash(*_: Any, **__: Any) -> dict[str, Any]:
        raise OSError("/Users/x/Fiktivbolaget AB/flags.json")

    monkeypatch.setattr(freeze, "freeze_run", crash)
    assert command.freeze_after_run(_args(), "dev", "r1", Path("out"), Path("rec")) == 1
    err = capsys.readouterr().err
    assert "not frozen: OSError" in err
    assert_no_fake_names(err)


def test_a_chmod_failure_after_the_append_still_reports_the_freeze(
    data_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ledger line is the freeze; a file that can't be made read-only afterwards is reported, not hidden behind a
    'not frozen' that the ledger contradicts."""
    outputs, record = make_run(data_root, "r1")

    def gone(*_: Any, **__: Any) -> None:
        raise FileNotFoundError("/Users/x/Fiktivbolaget AB/flags.json")

    monkeypatch.setattr(freeze.os, "chmod", gone)
    code = command.freeze_after_run(_args(), "dev", "r1", outputs, record)
    captured = capsys.readouterr()
    assert code == 1
    assert "frozen: ledger line 1, " in captured.out and "not frozen" not in captured.err
    assert "2 output files could not be made read-only (FileNotFoundError)" in captured.err
    assert is_frozen("co-001", "t-0001")
    assert_no_fake_names(captured.out, captured.err)


def test_an_unreadable_subfolder_is_refused_not_skipped(data_root: Path) -> None:
    """A folder the walk can't list would leave its files out of the hashes while the freeze looked complete."""
    outputs, record = make_run(data_root, "r1")
    hidden = outputs / "report"
    hidden.chmod(0o000)
    try:
        with pytest.raises(FreezeError, match="couldn't be read") as caught:
            freeze_run("co-001", "r1", outputs_dir=outputs, run_record_dir=record, tender_id="t-0001", now=NOW)
    finally:
        hidden.chmod(0o700)
    assert "report" not in str(caught.value)
    assert not ledger_file("co-001").exists()


def test_the_ledger_holds_no_customer_file_or_folder_name(data_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Only the harness's own run folders and file names go in clear; any other name is stored as its hash, so a
    freeze of a folder the customer named still proves its files without writing their names down."""
    outputs, record = make_run(data_root, "r1")
    (outputs / "Fiktivbolaget AB anbud.json").write_text("[]\n")
    kalkyl = data_root / "co-001" / "kalkyl" / "Påhittade Fastigheter"
    kalkyl.mkdir(parents=True)
    (kalkyl / "Örjan Exempelsson.json").write_text("[]\n")
    make_run(data_root, "r2")
    assert cli.main(["freeze", "run", "co-001", "r1"]) == 0
    assert cli.main(["freeze", "run", "co-001", "r2", "--outputs", str(kalkyl)]) == 0
    capsys.readouterr()
    ledger = ledger_file("co-001").read_text(encoding="utf-8")
    assert_no_fake_names(ledger)
    first, second = (json.loads(line) for line in ledger.splitlines())
    assert first["outputs_dir"] == "co-001/dev/t-0001/runs/r1"
    named = _sha(b"Fiktivbolaget AB anbud.json")
    assert set(first["outputs_sha256"]) == {"flags.json", "report/granskning.html", f"sha256:{named}"}
    assert second["outputs_dir"] == "sha256:" + _sha("co-001/kalkyl/Påhittade Fastigheter".encode())
    assert list(second["outputs_sha256"]) == ["sha256:" + _sha("Örjan Exempelsson.json".encode())]
    assert second["run_record_dir"] == "co-001/runs/r2"


def test_a_grown_hand_made_file_extends_a_ledger_that_holds_only_its_earlier_lines(
    data_root: Path, tmp_path: Path
) -> None:
    source = manual_file(tmp_path)
    assert import_manual("co-001", source) == 2
    last = source.read_bytes().splitlines()[-1]
    third = json.dumps({"kind": "manual_freeze_v0", "tender": "co-001/t-0003", "run_id": "hand-3", "prev": _sha(last)})
    with source.open("ab") as handle:
        handle.write(third.encode() + b"\n")
    assert import_manual("co-001", source) == 1
    ledger = ledger_file("co-001")
    assert ledger.read_bytes() == source.read_bytes() and verify(ledger).lines == 3
    run = make_run(data_root, "r-1", "t-0001", "co-001")  # once the harness has frozen a run, the file can't grow in
    freeze_run("co-001", "r-1", outputs_dir=run[0], run_record_dir=run[1], tender_id="t-0001")
    fourth = json.dumps({"kind": "manual_freeze_v0", "tender": "co-001/t-0004", "prev": _sha(third.encode())})
    with source.open("ab") as handle:
        handle.write(fourth.encode() + b"\n")
    with pytest.raises(FreezeError, match="other lines"):
        import_manual("co-001", source)
