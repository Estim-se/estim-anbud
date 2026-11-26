from __future__ import annotations

import csv
import fcntl
import hashlib
import io
import os
import stat
import threading
import tomllib
import unicodedata
import zipfile
from pathlib import Path

import pytest
from conftest import assert_no_fake_names

from anbud import cli, paths
from anbud import intake as intake_module
from anbud.evals import split_heldout as sh
from anbud.intake import IntakeError, dump_registry, intake
from anbud.prep.tender import prepare

# Invented folder names, deliberately containing the fake names the output must never show.
FOLDERS = {
    "Fiktivbolaget kv Ett": ("Fiktivbolaget AB", "Ägare Ett", "Familj A", "no", "yes"),
    "Örjan Exempelsson etapp 2": ("Byggare Tvåan", "Ägare Tvåan", "Familj Tvåan", "no", "no"),  # held-out
    "Påhittade Fastigheter hus 3": ("Byggare Fyran", "Ägare Fyran", "Familj Fyran", "no", "no"),  # dev
    "Tender fyra": ("Byggare Trean", "Ägare Trean", "Familj Trean", "no", "no"),  # held-out
    "Tender fem": ("Byggare Femman", "Ägare Femman", "Familj Femman", "no", "no"),  # dev
    "Kommande sex": ("", "", "", "yes", "no"),
}
DEV_FOLDER = "Fiktivbolaget kv Ett"  # forced dev


def zip_bytes(entries: dict[str, bytes | str], **kwargs: object) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", **kwargs) as zf:  # type: ignore[arg-type]
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def write_csv(incoming: Path, folders: dict[str, tuple[str, ...]]) -> None:
    with (incoming / "metadata.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["folder", "client", "property_owner", "project_family", "prospective", "forced_dev"])
        for name, row in folders.items():
            writer.writerow([name, *row])


def make_folder(incoming: Path, name: str) -> Path:
    docs = incoming / name / "docs"
    docs.mkdir(parents=True)
    (docs / "beskrivning.txt").write_text(f"Beskrivning för {name}.", encoding="utf-8")
    (docs / "ritningar.zip").write_bytes(zip_bytes({"ritningar/plan.txt": "plan", "../utanfor.txt": "escape"}))
    (incoming / name / "calc").mkdir()
    (incoming / name / "calc" / "kalkyl.txt").write_text("kalkyl", encoding="utf-8")
    return incoming / name


def build_incoming(root: Path, folders: dict[str, tuple[str, ...]] = FOLDERS) -> Path:
    incoming = root / "incoming" / "co-002"
    incoming.mkdir(parents=True, exist_ok=True)
    for name in folders:
        make_folder(incoming, name)
    write_csv(incoming, folders)
    return incoming


def expected_status() -> dict[str, str]:
    rows = [
        sh.TenderRow(f"t-{i:04d}", sh.group_keys(c, o, f), p == "yes", d == "yes")
        for i, (c, o, f, p, d) in enumerate(FOLDERS.values(), 1)
    ]
    state = sh.LedgerState()
    for event in sh.plan_split(state, rows):
        state.status[event["tender"]] = event["status"]
    return state.status


def tree_hash(folder: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        digest.update(path.relative_to(folder).as_posix().encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def only_dev(data_root: Path, name: str = DEV_FOLDER) -> tuple[Path, Path]:
    """An incoming folder with the one forced-dev tender."""
    return build_incoming(data_root, {name: FOLDERS[DEV_FOLDER]}), paths.dev_tender_dir("co-002", "t-0001")


def test_intake_splits_first_and_never_reads_held_out_files(data_root: Path) -> None:
    incoming = build_incoming(data_root)
    status = expected_status()
    assert sh.HELDOUT in status.values() and sh.DEV in status.values()
    ids = {f"t-{i:04d}": name for i, name in enumerate(FOLDERS, 1)}
    for tid, name in ids.items():
        if status[tid] != sh.DEV:  # make held-out and prospective files unreadable: any read would fail
            for f in (incoming / name).rglob("*"):
                if f.is_file():
                    f.chmod(0)
    result = intake("co-002", incoming, new_ledger=True, company_name="Kund")
    assert not result.failed
    assert result.dev + result.heldout + result.prospective == len(FOLDERS) and result.prospective == 1
    assert sh.load_ledger(paths.ledger_path("co-002")).status == status
    registry = tomllib.loads((data_root / "registry.toml").read_text(encoding="utf-8"))
    assert registry["companies"]["co-002"] == "Kund"
    assert registry["tenders"]["co-002"] == ids
    assert set(registry["hashes"]["co-002"]) == set(ids)
    assert stat.S_IMODE(os.stat(data_root / "registry.toml").st_mode) == 0o600
    for tid in status:
        if status[tid] == sh.DEV:
            tender = paths.dev_tender_dir("co-002", tid)
            assert (tender / "inbox" / "beskrivning.txt").exists()
            assert (tender / "inbox" / "ritningar" / "ritningar" / "plan.txt").read_text() == "plan"
            assert not (tender / "inbox" / "ritningar.zip").exists()  # unpacked, so not in the inbox twice
            assert not list(tender.rglob("utanfor.txt"))  # the escaping entry wasn't unpacked
            fingerprint = registry["hashes"]["co-002"][tid]
            assert (tender / "originals" / fingerprint / "docs" / "ritningar.zip").exists()  # the delivery, whole
            assert (tender / "calc" / "kalkyl.txt").exists()
            manifest = (tender / "inbox.sha256").read_text().splitlines()
            assert sorted(line.split("  ", 1)[1] for line in manifest) == [
                "beskrivning.txt",
                "ritningar/ritningar/plan.txt",
            ]
            assert (tender / "calc.sha256").exists()
        else:
            assert (data_root / "heldout" / "co-002" / tid / "docs" / "beskrivning.txt").exists()
    assert sorted(p.name for p in incoming.iterdir()) == ["metadata.csv"]
    for f in (data_root / "heldout").rglob("*"):
        if f.is_file():
            f.chmod(0o600)  # let pytest clean up


def test_held_out_folders_are_byte_identical_after_the_move(data_root: Path) -> None:
    incoming = build_incoming(data_root)
    before = {name: tree_hash(incoming / name) for name in FOLDERS}
    intake("co-002", incoming, new_ledger=True)
    status = sh.load_ledger(paths.ledger_path("co-002")).status
    for i, name in enumerate(FOLDERS, 1):
        tid = f"t-{i:04d}"
        if status[tid] != sh.DEV:
            assert tree_hash(data_root / "heldout" / "co-002" / tid) == before[name]


def test_a_second_intake_of_the_same_folder_set_is_a_no_op(data_root: Path) -> None:
    incoming = build_incoming(data_root)
    intake("co-002", incoming, new_ledger=True)
    again = intake("co-002", incoming)
    assert again.counts() == {"dev": 0, "heldout": 0, "prospective": 0, "burned": 0} and not again.failed


def test_every_folder_needs_a_metadata_row(data_root: Path) -> None:
    incoming = build_incoming(data_root)
    (incoming / "Extra mapp").mkdir()
    with pytest.raises(IntakeError, match="1 folder"):
        intake("co-002", incoming, new_ledger=True)
    assert not (data_root / "registry.toml").exists()


def test_the_first_intake_needs_new_ledger_and_only_the_first(data_root: Path) -> None:
    incoming = build_incoming(data_root)
    with pytest.raises(IntakeError, match="new-ledger"):
        intake("co-002", incoming)
    intake("co-002", incoming, new_ledger=True)
    with pytest.raises(IntakeError, match="already has a ledger"):
        intake("co-002", incoming, new_ledger=True)


def test_cli_prints_counts_only(data_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    incoming = build_incoming(data_root)
    assert cli.main(["intake", "co-002", str(incoming), "--new-ledger"]) == 0
    printed = capsys.readouterr()
    assert "dev" in printed.out and "zips: 3 unpacked" in printed.out
    assert_no_fake_names(printed.out, printed.err)


# Finding 1: one ignore rule for intake's manifest and prep.


def test_mac_and_windows_extras_dont_make_prep_refuse_the_tender(data_root: Path) -> None:
    incoming, tender = only_dev(data_root)
    docs = incoming / DEV_FOLDER / "docs"
    (docs / ".DS_Store").write_bytes(b"\0\0\0\1Bud1")
    (docs / "__MACOSX").mkdir()
    (docs / "__MACOSX" / "._beskrivning.txt").write_bytes(b"\0\5\26\7")
    (docs / "Thumbs.db").write_bytes(b"thumbs")
    (docs / "bilaga.zip").write_bytes(
        zip_bytes({"bilaga/text.txt": "text", "__MACOSX/bilaga/._text.txt": "fork", "bilaga/.DS_Store": "ds"})
    )
    result = intake("co-002", incoming, new_ledger=True)
    assert not result.failed and result.dev == 1
    assert result.unpack.entries_skipped == 3  # two system files, and the escaping entry in ritningar.zip
    summary = prepare("co-002", tender / "inbox", tender / "prep", tender / "inbox.sha256")
    assert summary["documents"] == 3  # beskrivning, plan, text; no system files


# Finding 2: odd zips never escape the CLI, and no message names a file.


def test_encrypted_deflate64_and_damaged_zips_are_kept_as_delivered(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    incoming, tender = only_dev(data_root)
    docs = incoming / DEV_FOLDER / "docs"
    encrypted = bytearray(zip_bytes({"Fiktivbolaget hemlig.txt": "x"}))
    for offset in (6, encrypted.rfind(b"PK\x01\x02") + 8):  # the encryption bit, in the local and central headers
        encrypted[offset] |= 0x01
    (docs / "krypterad.zip").write_bytes(bytes(encrypted))
    deflate64 = bytearray(zip_bytes({"Fiktivbolaget d64.txt": "x"}))
    for offset in (8, deflate64.rfind(b"PK\x01\x02") + 10):  # compression method 9: Deflate64
        deflate64[offset] = 9
    (docs / "d64.zip").write_bytes(bytes(deflate64))
    damaged = bytearray(zip_bytes({"Fiktivbolaget trasig.txt": "a" * 64}))
    damaged[30 + len("Fiktivbolaget trasig.txt") + 3] ^= 0xFF  # stored data: the CRC no longer matches
    (docs / "trasig.zip").write_bytes(bytes(damaged))
    assert cli.main(["intake", "co-002", str(incoming), "--new-ledger"]) == 0
    printed = capsys.readouterr()
    assert "3 kept as delivered" in printed.out
    assert_no_fake_names(printed.out, printed.err)
    kept = {p.name for p in (tender / "inbox").iterdir()}
    assert {"krypterad.zip", "d64.zip", "trasig.zip"} <= kept
    assert not list((tender / "inbox").rglob("Fiktivbolaget*"))  # nothing half-unpacked


def test_a_bad_csv_or_any_unexpected_error_prints_no_name(
    data_root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    incoming = build_incoming(data_root)
    (incoming / "metadata.csv").write_bytes(b"folder,client\nFiktivbolaget\0,x\n")
    assert cli.main(["intake", "co-002", str(incoming), "--new-ledger"]) == cli.REFUSED
    printed = capsys.readouterr()
    assert_no_fake_names(printed.out, printed.err)

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError(f"cannot open {incoming / 'Fiktivbolaget kv Ett'}")

    monkeypatch.setattr(intake_module, "intake", boom)
    assert cli.main(["intake", "co-002", str(incoming)]) == cli.REFUSED
    printed = capsys.readouterr()
    assert "OSError" in printed.err
    assert_no_fake_names(printed.out, printed.err)


# Finding 3: a failed placement is rolled back, and a rerun places the tender.


def test_a_failure_partway_rolls_back_and_a_rerun_places_the_tender(
    data_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    incoming, tender = only_dev(data_root)
    before = tree_hash(incoming / DEV_FOLDER)

    def fail(*args: object) -> None:
        raise OSError(f"disk full writing {incoming / 'Fiktivbolaget'}")

    with monkeypatch.context() as patched:  # scoped: undo() would also undo the data root the fixture set
        patched.setattr(intake_module, "unpack_all", fail)
        assert cli.main(["intake", "co-002", str(incoming), "--new-ledger"]) == 1
    printed = capsys.readouterr()
    assert "t-0001: OSError" in printed.err
    assert_no_fake_names(printed.out, printed.err)
    assert not tender.exists() and not list(tender.parent.iterdir())  # no tender, no staging left
    assert tree_hash(incoming / DEV_FOLDER) == before  # the delivery is untouched
    ledger = paths.ledger_path("co-002").read_bytes()

    result = intake("co-002", incoming)
    assert result.dev == 1 and not result.failed
    assert paths.ledger_path("co-002").read_bytes() == ledger  # same id, no new events
    assert (tender / "inbox.sha256").exists()


def test_a_crash_between_the_two_renames_is_recovered(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    incoming, tender = only_dev(data_root)
    real_rename = os.rename

    def crash(src: object, dst: object) -> None:
        if Path(dst) == tender:
            raise KeyboardInterrupt  # the process dies: nothing in intake gets to clean up
        real_rename(src, dst)

    with monkeypatch.context() as patched, pytest.raises(KeyboardInterrupt):
        patched.setattr(os, "rename", crash)
        intake("co-002", incoming, new_ledger=True)
    # The rollback put the delivery back before the interrupt propagated.
    assert (incoming / DEV_FOLDER).is_dir() and not tender.exists()

    staging = tender.parent / ".staging-t-0001"
    (staging / "originals").mkdir(parents=True)  # now the harder case: killed with the delivery inside staging
    fingerprint = intake_module.folder_fingerprint(incoming / DEV_FOLDER)
    os.rename(incoming / DEV_FOLDER, staging / "originals" / fingerprint)
    result = intake("co-002", incoming)
    assert result.dev == 1 and not result.failed and not staging.exists()
    assert (tender / "originals" / fingerprint / "docs" / "beskrivning.txt").exists()


# Finding 4: originals by hash, so two zips with one name never overwrite each other.


def test_two_zips_with_one_name_both_survive(data_root: Path) -> None:
    incoming, tender = only_dev(data_root)
    docs = incoming / DEV_FOLDER / "docs"
    for part in ("a", "b"):
        (docs / part).mkdir()
        (docs / part / "bilagor.zip").write_bytes(zip_bytes({f"{part}.txt": part}))
    intake("co-002", incoming, new_ledger=True)
    assert (tender / "inbox" / "a" / "bilagor" / "a.txt").read_text() == "a"
    assert (tender / "inbox" / "b" / "bilagor" / "b.txt").read_text() == "b"
    (delivered,) = (tender / "originals").iterdir()
    assert {p.read_bytes() for p in delivered.rglob("bilagor.zip")} == {
        zip_bytes({"a.txt": "a"}),
        zip_bytes({"b.txt": "b"}),
    }


# Finding 5: only .zip files are unpacked; nested zips are unpacked too.


def test_only_zip_files_are_unpacked_and_nested_zips_too(data_root: Path) -> None:
    incoming, tender = only_dev(data_root)
    docs = incoming / DEV_FOLDER / "docs"
    (docs / "bilder.pptx").write_bytes(zip_bytes({"ppt/presentation.xml": "<p/>"}))
    (docs / "text.odt").write_bytes(zip_bytes({"content.xml": "<c/>"}))
    inner = zip_bytes({"djup.txt": "djup"})
    (docs / "YTTRE.ZIP").write_bytes(zip_bytes({"mitten.zip": zip_bytes({"inre.zip": inner})}))
    result = intake("co-002", incoming, new_ledger=True)
    inbox = tender / "inbox"
    assert (inbox / "bilder.pptx").is_file() and (inbox / "text.odt").is_file()
    assert (inbox / "YTTRE" / "mitten" / "inre" / "djup.txt").read_text() == "djup"
    assert not list(inbox.rglob("*.zip")) and not list(inbox.rglob("*.ZIP"))
    assert result.unpack.archives_unpacked == 4  # ritningar, YTTRE, mitten, inre


def test_zips_nested_too_deep_are_kept(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(intake_module, "MAX_DEPTH", 1)
    incoming, tender = only_dev(data_root)
    (incoming / DEV_FOLDER / "docs" / "yttre.zip").write_bytes(zip_bytes({"inre.zip": zip_bytes({"x.txt": "x"})}))
    result = intake("co-002", incoming, new_ledger=True)
    assert result.unpack.archives_kept == 1 and (tender / "inbox" / "yttre" / "inre.zip").is_file()


def test_zip_names_without_the_utf8_flag(data_root: Path) -> None:
    incoming, tender = only_dev(data_root)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Ägare.txt", "mac")  # written as UTF-8 with the flag; the flag is cleared below
    raw = bytearray(buf.getvalue())
    for marker in (b"PK\x03\x04", b"PK\x01\x02"):
        at = raw.find(marker)
        flags = 6 if marker == b"PK\x03\x04" else 8
        raw[at + flags + 1] &= ~0x08  # clear the UTF-8 flag zipfile set
    (incoming / DEV_FOLDER / "docs" / "namn.zip").write_bytes(bytes(raw))
    intake("co-002", incoming, new_ledger=True)
    names = {unicodedata.normalize("NFC", p.name) for p in (tender / "inbox" / "namn").iterdir()}
    assert names == {"Ägare.txt"}


# Finding 6: deliveries are matched by fingerprint; a new delivery under a registered name is refused.


def test_a_new_delivery_under_a_registered_name_is_refused_before_any_write(data_root: Path) -> None:
    incoming = build_incoming(data_root)
    intake("co-002", incoming, new_ledger=True)
    ledger = paths.ledger_path("co-002").read_bytes()
    registry = (data_root / "registry.toml").read_bytes()
    folder = make_folder(incoming, "Tender fyra")  # the name of a held-out tender, new content
    (folder / "docs" / "annat.txt").write_text("en annan upphandling", encoding="utf-8")
    with pytest.raises(IntakeError, match="name of a registered tender"):
        intake("co-002", incoming)
    assert paths.ledger_path("co-002").read_bytes() == ledger
    assert (data_root / "registry.toml").read_bytes() == registry
    assert folder.is_dir()


def test_the_same_delivery_again_is_recognised(data_root: Path) -> None:
    incoming = build_incoming(data_root)
    intake("co-002", incoming, new_ledger=True)
    ledger = paths.ledger_path("co-002").read_bytes()
    make_folder(incoming, "Tender fem")  # identical names and sizes
    result = intake("co-002", incoming)
    assert result.already_in == 1 and result.counts()["dev"] == 0
    assert paths.ledger_path("co-002").read_bytes() == ledger
    assert (incoming / "Tender fem").is_dir()  # left where it is


# Finding 7: one lock for the registry across companies, and temp files of their own.


def test_intake_waits_for_the_registry_lock(data_root: Path) -> None:
    incoming, tender = only_dev(data_root)
    fd = os.open(data_root / "registry.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    worker = threading.Thread(target=intake, args=("co-002", incoming), kwargs={"new_ledger": True})
    worker.start()
    worker.join(0.5)
    assert worker.is_alive() and not (data_root / "registry.toml").exists()
    os.close(fd)
    worker.join(10)
    assert not worker.is_alive() and tender.exists()


def test_concurrent_registry_writes_never_share_a_temp_file(data_root: Path) -> None:
    target = data_root / "registry.toml"
    errors: list[BaseException] = []

    def write(n: int) -> None:
        try:
            for i in range(50):
                intake_module._write_private(target, f"# {n} {i}\n")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    workers = [threading.Thread(target=write, args=(n,)) for n in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    assert not errors and target.read_text().startswith("# ")
    assert [p.name for p in data_root.iterdir()] == ["registry.toml"]


# Finding 8: a dangling ledger link is refused, and nothing is created at its target.


def test_a_dangling_ledger_link_is_refused(data_root: Path, tmp_path: Path) -> None:
    incoming, _ = only_dev(data_root)
    ledger = paths.ledger_path("co-002")
    ledger.parent.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere.jsonl"
    ledger.symlink_to(elsewhere)
    with pytest.raises(IntakeError, match="link"):
        intake("co-002", incoming, new_ledger=True)
    assert not elsewhere.exists()


# Nits: names compared in NFC; DEL escaped in the registry.


def test_decomposed_folder_names_match_their_row(data_root: Path) -> None:
    name = "Örjan Exempelsson etapp 2"
    incoming = data_root / "incoming" / "co-002"
    incoming.mkdir(parents=True)
    make_folder(incoming, unicodedata.normalize("NFD", name))
    write_csv(incoming, {name: FOLDERS[DEV_FOLDER]})
    result = intake("co-002", incoming, new_ledger=True)
    assert result.dev == 1
    registry = tomllib.loads((data_root / "registry.toml").read_text(encoding="utf-8"))
    assert registry["tenders"]["co-002"]["t-0001"] == name  # stored composed


def test_registry_round_trips_control_characters_and_keeps_other_tables() -> None:
    registry = {
        "companies": {"co-001": "Namn\x7f med\tDEL", "co-002": 'citat " och \\'},
        "tenders": {"co-001": {"t-0001": "a\x01b"}},
        "notes": {"x": "y"},
    }
    assert tomllib.loads(dump_registry(registry)) == registry
    with pytest.raises(IntakeError):
        dump_registry({"companies": {"co-001": 1}})
