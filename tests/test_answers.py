"""The answer store: only for frozen tenders, stored by hash, held-out answers in sealed storage. Synthetic files."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest
from conftest import assert_no_fake_names
from test_freeze import make_run
from test_runguard import ledger_with

from anbud import cli, paths
from anbud.evals import split_heldout as sh
from anbud.evals.answers import AnswerError, add, status
from anbud.evals.freeze import freeze_run


def _frozen_dev(data_root: Path, tender: str = "t-0001") -> None:
    outputs, record = make_run(data_root, f"r-{tender}", tender)
    freeze_run("co-001", f"r-{tender}", outputs_dir=outputs, run_record_dir=record, tender_id=tender)


def _file(folder: Path, name: str, data: bytes) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return path


def test_an_unfrozen_tender_takes_no_answers(data_root: Path, tmp_path: Path) -> None:
    ledger_with(data_root, t_0001=sh.DEV)
    award = _file(tmp_path / "in", "beslut.pdf", b"syntetiskt beslut")
    assert cli.main(["answers", "add", "co-001", "t-0001", str(award), "--kind", "award"]) != 0
    with pytest.raises(AnswerError, match="no frozen prediction"):
        add("co-001", "t-0001", [award], "award")
    assert not paths.answers_dir("co-001", "t-0001").exists()


def test_an_unknown_tender_or_kind_is_refused(data_root: Path, tmp_path: Path) -> None:
    ledger_with(data_root, t_0001=sh.DEV)
    award = _file(tmp_path / "in", "beslut.pdf", b"x")
    with pytest.raises(AnswerError, match="split ledger"):
        add("co-001", "t-0009", [award], "award")
    with pytest.raises(AnswerError, match="kind"):
        add("co-001", "t-0001", [award], "guess")


def test_after_a_freeze_answers_are_stored_by_hash_and_duplicates_skipped(data_root: Path, tmp_path: Path) -> None:
    ledger_with(data_root, t_0001=sh.DEV)
    _frozen_dev(data_root)
    award = _file(tmp_path / "in", "Beslut.PDF", b"syntetiskt beslut")
    protocol = _file(tmp_path / "in", "protokoll.xlsx", b"syntetiskt protokoll")
    copy = _file(tmp_path / "other", "samma.pdf", b"syntetiskt beslut")
    first = add("co-001", "t-0001", [award, protocol, copy], "award")
    assert (first.added, first.skipped) == (2, 1)
    sha = hashlib.sha256(b"syntetiskt beslut").hexdigest()
    store = paths.answers_dir("co-001", "t-0001")
    stored = store / "files" / f"{sha}.pdf"
    assert stored.read_bytes() == b"syntetiskt beslut" and stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert not [p for p in (store / "files").iterdir() if p.name.startswith(".tmp-")]
    index = [json.loads(line) for line in (store / "index.jsonl").read_text().splitlines()]
    assert [e["sha256"] for e in index] == [sha, hashlib.sha256(b"syntetiskt protokoll").hexdigest()]
    assert index[0]["suffix"] == ".pdf" and index[0]["size"] == 17 and index[0]["kind"] == "award"
    again = add("co-001", "t-0001", [award], "award")
    assert (again.added, again.skipped) == (0, 1)
    current = status("co-001", "t-0001")
    assert current.frozen and current.split == sh.DEV and current.kinds == {"award": 2} and current.total == 2


def test_symlinks_folders_and_sealed_sources_are_refused_and_nothing_is_added(data_root: Path, tmp_path: Path) -> None:
    ledger_with(data_root, t_0001=sh.DEV)
    _frozen_dev(data_root)
    good = _file(tmp_path / "in", "a.pdf", b"a")
    link = tmp_path / "in" / "b.pdf"
    link.symlink_to(good)
    with pytest.raises(AnswerError, match="file 2 of 2: a symlink"):
        add("co-001", "t-0001", [good, link], "qa")
    with pytest.raises(AnswerError, match="file 2 of 2: a folder"):
        add("co-001", "t-0001", [good, tmp_path / "in"], "qa")
    sealed = _file(data_root / "heldout" / "co-001", "c.pdf", b"c")
    with pytest.raises(AnswerError, match="held-out or sealed"):
        add("co-001", "t-0001", [good, sealed], "qa")
    store = paths.answers_dir("co-001", "t-0001")
    assert not (store / "index.jsonl").exists()
    assert not (store / "files").exists() or not list((store / "files").iterdir())


def test_a_symlinked_store_is_refused(data_root: Path, tmp_path: Path) -> None:
    ledger_with(data_root, t_0001=sh.DEV)
    _frozen_dev(data_root)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (data_root / "answers" / "co-001").mkdir(parents=True)
    (data_root / "answers" / "co-001" / "t-0001").symlink_to(elsewhere)
    with pytest.raises(AnswerError, match="symlink"):
        add("co-001", "t-0001", [_file(tmp_path / "in", "a.pdf", b"a")], "qa")
    assert not list(elsewhere.iterdir())


def test_held_out_answers_go_to_sealed_storage_and_need_the_sealed_ledger(data_root: Path, tmp_path: Path) -> None:
    ledger_with(data_root, t_0001=sh.DEV, t_0002=sh.HELDOUT)
    award = _file(tmp_path / "in", "beslut.pdf", b"syntetiskt beslut")
    _frozen_dev(data_root, "t-0002")  # a dev-ledger freeze doesn't count for a held-out tender
    with pytest.raises(AnswerError, match="no frozen prediction"):
        add("co-001", "t-0002", [award], "award")
    outputs, record = make_run(data_root, "r-sealed", "t-0002")
    freeze_run("co-001", "r-sealed", outputs_dir=outputs, run_record_dir=record, tender_id="t-0002", sealed=True)
    assert add("co-001", "t-0002", [award], "award").added == 1
    sealed = data_root / "sealed" / "answers" / "co-001" / "t-0002"
    assert (sealed / "index.jsonl").exists() and not paths.answers_dir("co-001", "t-0002").exists()
    assert status("co-001", "t-0002").split == sh.HELDOUT


def test_cli_answers_print_counts_and_hashes_never_names(
    data_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger_with(data_root, t_0001=sh.DEV)
    _frozen_dev(data_root)
    named = _file(tmp_path / "Påhittade Fastigheter", "Fiktivbolaget AB tilldelning Örjan.pdf", b"syntetiskt")
    link = tmp_path / "Exempelsson länk.pdf"
    link.symlink_to(named)
    assert cli.main(["answers", "add", "co-001", "t-0001", str(named), "--kind", "award"]) == 0
    assert cli.main(["answers", "add", "co-001", "t-0001", str(named), "--kind", "award"]) == 0
    assert cli.main(["answers", "add", "co-001", "t-0001", str(link), "--kind", "award"]) == cli.REFUSED
    missing = tmp_path / "Fiktivbolaget saknas.pdf"
    assert cli.main(["answers", "add", "co-001", "t-0001", str(missing), "--kind", "qa"]) == cli.REFUSED
    assert cli.main(["answers", "status", "co-001", "t-0001"]) == 0
    captured = capsys.readouterr()
    assert "1 added, 0 already stored" in captured.out and "0 added, 1 already stored" in captured.out
    assert "dev, frozen; 1 files; kinds: award 1" in captured.out
    assert hashlib.sha256(b"syntetiskt").hexdigest()[:12] in captured.out
    assert_no_fake_names(captured.out, captured.err)


def test_an_unreadable_source_leaks_no_descriptor_or_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import tempfile

    from anbud.evals import answers

    store = tmp_path / "files"
    store.mkdir()
    source = _file(tmp_path / "in", "beslut.pdf", b"syntetiskt beslut")
    source.chmod(0o000)
    made: list[int] = []
    real = tempfile.mkstemp

    def tracked(*args: object, **kw: object) -> tuple[int, str]:
        fd, name = real(*args, **kw)  # type: ignore[call-overload]
        made.append(fd)
        return fd, name

    monkeypatch.setattr(answers.tempfile, "mkstemp", tracked)
    try:
        with pytest.raises(PermissionError):
            answers._stage(source, store, 1, 1)
    finally:
        source.chmod(0o600)
    assert made and list(store.iterdir()) == []
    with pytest.raises(OSError):
        os.fstat(made[0])  # closed
