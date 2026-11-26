from __future__ import annotations

import csv
import io
import os
import stat
import tomllib
import zipfile
from pathlib import Path

import pytest
from conftest import assert_no_fake_names

from anbud import cli, paths
from anbud.evals import split_heldout as sh
from anbud.intake import IntakeError, intake

# Invented folder names, deliberately containing the fake names the output must never show.
FOLDERS = {
    "Fiktivbolaget kv Ett": ("Fiktivbolaget AB", "Ägare Ett", "Familj A", "no", "yes"),
    "Örjan Exempelsson etapp 2": ("Byggare Tvåan", "Ägare Tvåan", "Familj Tvåan", "no", "no"),  # held-out
    "Påhittade Fastigheter hus 3": ("Byggare Fyran", "Ägare Fyran", "Familj Fyran", "no", "no"),  # dev
    "Tender fyra": ("Byggare Trean", "Ägare Trean", "Familj Trean", "no", "no"),  # held-out
    "Tender fem": ("Byggare Femman", "Ägare Femman", "Familj Femman", "no", "no"),  # dev
    "Kommande sex": ("", "", "", "yes", "no"),
}


def build_incoming(root: Path) -> Path:
    incoming = root / "incoming" / "Kund"
    for name in FOLDERS:
        docs = incoming / name / "docs"
        docs.mkdir(parents=True)
        (docs / "beskrivning.txt").write_text(f"Beskrivning för {name}.", encoding="utf-8")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("ritningar/plan.txt", "plan")
            zf.writestr("../utanfor.txt", "escape")
        (docs / "ritningar.zip").write_bytes(buf.getvalue())
        (incoming / name / "calc").mkdir()
        (incoming / name / "calc" / "kalkyl.txt").write_text("kalkyl", encoding="utf-8")
    with (incoming / "metadata.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["folder", "client", "property_owner", "project_family", "prospective", "forced_dev"])
        for name, row in FOLDERS.items():
            writer.writerow([name, *row])
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
    moved = intake("co-002", incoming, new_ledger=True)
    assert moved["dev"] + moved["heldout"] + moved["prospective"] == len(FOLDERS) and moved["prospective"] == 1
    assert sh.load_ledger(paths.ledger_path("co-002")).status == status
    registry = tomllib.loads((data_root / "registry.toml").read_text(encoding="utf-8"))
    assert registry["companies"]["co-002"] == "Kund"
    assert registry["tenders"]["co-002"] == ids
    assert stat.S_IMODE(os.stat(data_root / "registry.toml").st_mode) == 0o600
    for tid in status:
        if status[tid] == sh.DEV:
            tender = paths.dev_tender_dir("co-002", tid)
            assert (tender / "inbox" / "beskrivning.txt").exists()
            assert (tender / "inbox" / "ritningar" / "ritningar" / "plan.txt").read_text() == "plan"
            assert not list(tender.rglob("utanfor.txt"))  # the escaping entry wasn't unpacked
            assert (tender / "originals" / "ritningar.zip").exists()
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


def test_a_second_intake_of_the_same_folder_set_is_a_no_op(data_root: Path) -> None:
    incoming = build_incoming(data_root)
    intake("co-002", incoming, new_ledger=True)
    assert intake("co-002", incoming) == {"dev": 0, "heldout": 0, "prospective": 0, "burned": 0}


def test_every_folder_needs_a_metadata_row(data_root: Path) -> None:
    incoming = build_incoming(data_root)
    (incoming / "Extra mapp").mkdir()
    with pytest.raises(IntakeError, match="1 folder"):
        intake("co-002", incoming, new_ledger=True)
    assert not (data_root / "registry.toml").exists()


def test_the_first_intake_needs_new_ledger(data_root: Path) -> None:
    with pytest.raises(IntakeError, match="new-ledger"):
        intake("co-002", build_incoming(data_root))


def test_cli_prints_counts_only(data_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    incoming = build_incoming(data_root)
    assert cli.main(["intake", "co-002", str(incoming), "--new-ledger"]) == 0
    printed = capsys.readouterr()
    assert "dev" in printed.out
    assert_no_fake_names(printed.out, printed.err)
