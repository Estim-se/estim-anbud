"""Case cards: counts for dev tenders, sealed tenders counted from the split ledger and never opened. Every tender,
file and run here is synthetic."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import assert_no_fake_names
from test_freeze import make_run

from anbud import cli, paths
from anbud.evals import cards
from anbud.evals import split_heldout as sh
from anbud.evals.freeze import freeze_run

NOW = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)
SECRET = "Fiktivbolaget hemlig rad"


def ledger(data_root: Path, company: str, statuses: dict[str, str]) -> None:
    sh.append_events(
        paths.ledger_path(company),
        [sh._assign(t, s, "test", (), prospective=s == sh.PROSPECTIVE) for t, s in statuses.items()],
    )


def dev_tender(data_root: Path, company: str, tender: str) -> Path:
    folder = data_root / company / "dev" / tender
    inbox = folder / "inbox"
    (inbox / "Fiktivbolaget ritningar").mkdir(parents=True)
    files = {"Fiktivbolaget AF.pdf": b"%PDF-1.4 x", "Fiktivbolaget ritningar/E-10.dwg": b"dwg", "lista.xlsx": b"x"}
    for name, data in files.items():
        (inbox / name).write_bytes(data)
    (folder / "inbox.sha256").write_text("".join(f"{'0' * 64}  {n}\n" for n in files))
    (folder / "calc").mkdir()
    (folder / "calc" / "kalkyl.xlsx").write_bytes(b"x")
    (folder / "calc.sha256").write_text(f"{'1' * 64}  kalkyl.xlsx\n")
    (folder / "prep").mkdir()
    summary = {
        "documents": 3,
        "documents_by_kind": {"pdf": 1, "dwg": 1, "xlsx": 1},
        "pages_by_kind": {"text": 1},
        "sheets": 1,
        "tiles": 2,
        "skipped": 1,
        "hidden_files_skipped": 0,
        "links_skipped": 0,
        "masked_by_kind": {"person": 2},
        "block_chars": 120,
    }
    (folder / "prep" / "summary.json").write_text(json.dumps(summary))
    (folder / "prep" / "blocks.jsonl").write_text(json.dumps({"text": SECRET}) + "\n")
    (folder / "intake.json").write_text(
        json.dumps(
            {
                "tender": tender,
                "notice": {"buyer": "Fiktivbolaget AB", "reference": "Dnr 1"},
                "found": {"buyer": True, "deadline": True},
                "held": {"qa": 2},
                "unpack": {"archives_unpacked": 1, "archives_kept": 0},
            }
        )
    )
    return folder


def test_cards_count_dev_tenders_and_only_count_sealed_ones(
    data_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger(data_root, "co-001", {"t-0001": sh.DEV, "t-0002": sh.HELDOUT, "t-0003": sh.PROSPECTIVE})
    dev_tender(data_root, "co-001", "t-0001")
    outputs, record = make_run(data_root, "r1")
    freeze_run("co-001", "r1", outputs_dir=outputs, run_record_dir=record, tender_id="t-0001", now=NOW)
    (data_root / "heldout" / "co-001" / "t-0002").mkdir(parents=True)
    real_iterdir, real_scandir = Path.iterdir, os.scandir

    def guarded_iterdir(self: Path) -> object:
        assert "heldout" not in self.parts and "sealed" not in self.parts  # sealed storage is never listed
        return real_iterdir(self)

    def guarded_scandir(path: object = ".") -> object:
        assert "heldout" not in Path(str(path)).parts and "sealed" not in Path(str(path)).parts
        return real_scandir(path)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)
    monkeypatch.setattr(os, "scandir", guarded_scandir)

    assert cli.main(["cards", "co-001"]) == 0
    printed = capsys.readouterr()
    assert "1 dev cards, 1 held-out and 1 prospective counted" in printed.out
    assert_no_fake_names(printed.out, printed.err)

    text = (data_root / "notes" / "co-001" / "case-cards.md").read_text(encoding="utf-8")
    assert "1 held-out and 1 prospective" in text and "### t-0001" in text
    assert "Files: 3" in text and "pdf 1" in text and "dwg 1" in text and "xlsx 1" in text
    assert "Calculation files: 1" in text
    assert "1 review, 0 Kalkyl; 1 frozen in the ledger" in text
    assert "Held as answer keys: qa 2" in text
    for leak in ("Fiktivbolaget", SECRET, "Dnr 1", "t-0002", "t-0003"):
        assert leak not in text  # no names, no text, no notice values, no sealed ids


def test_a_company_with_no_dev_tender_gets_counts_only(data_root: Path) -> None:
    ledger(data_root, "co-002", {"t-0001": sh.HELDOUT, **{f"t-000{i}": sh.PROSPECTIVE for i in range(2, 7)}})
    target, built = cards.write("co-002", now=NOW)
    assert built["split"] == {"dev": 0, "heldout": 1, "prospective": 5}
    text = target.read_text(encoding="utf-8")
    assert "1 held-out and 5 prospective" in text and "## Dev tenders (0)" in text
    assert "t-000" not in text


def test_a_linked_notes_folder_is_refused(data_root: Path, tmp_path: Path) -> None:
    ledger(data_root, "co-002", {"t-0001": sh.HELDOUT})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (data_root / "notes").mkdir()
    (data_root / "notes" / "co-002").symlink_to(elsewhere)
    with pytest.raises(cards.CardError, match="link"):
        cards.write("co-002", now=NOW)
    assert cli.main(["cards", "co-002"]) == cli.REFUSED
    assert list(elsewhere.iterdir()) == []
