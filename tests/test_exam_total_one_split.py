"""The paid exam's one total carries no count of one split's runs (audit 2, finding E4, repro a2_3).

The total printed figures over both splits and, beside them, the prospective runs that finished after their tender's
deadline. With every prospective tender past its deadline that count was every finished prospective run, so finished
minus it gave the held-out split's finished count. Whether a run came after its deadline stays on its freeze line,
for scoring, and out of the total. Synthetic store, fake engines, temp data root; no model call.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

import pytest
from test_exam import LOCAL_NOW, NOW, PDFS, FakeEngines, register, tender_zip

from anbud import paths
from anbud.evals import exam, freeze
from anbud.evals import split_heldout as sh

COSTS = {"granskning": 8.0, "kalkyl": 1.0, "baseline": 2.0}
HELD = ("t-0001", "t-0004")
PAST = {"t-0001": "2026-06-01", "t-0002": "2026-11-25 12:00", "t-0003": "2026-11-20"}
OPEN = {"t-0001": "2026-06-01", "t-0002": "2026-12-20 12:00", "t-0003": "2026-12-22"}


def _store(data_root: Path, deadlines: dict[str, str]) -> None:
    """Two prospective tenders (t-0002, t-0003) with the given deadlines; two held-out tenders (a zip and a folder)."""
    statuses = {"t-0001": sh.HELDOUT, "t-0002": sh.PROSPECTIVE, "t-0003": sh.PROSPECTIVE, "t-0004": sh.HELDOUT}
    sh.append_events(
        paths.ledger_path("co-002"),
        [sh._assign(t, s, "test", (), prospective=s == sh.PROSPECTIVE) for t, s in statuses.items()],
    )
    for t, deadline in deadlines.items():
        folder = data_root / "heldout" / "co-002" / t
        data = tender_zip(deadline)
        (folder / "originals").mkdir(parents=True)
        (folder / "originals" / f"{hashlib.sha256(data).hexdigest()}.zip").write_bytes(data)
        iso = datetime.fromisoformat(deadline.replace(" ", "T")).isoformat()
        (folder / "intake.json").write_text(json.dumps({"tender": t, "notice": {"deadline": iso}}))
    folder = data_root / "heldout" / "co-002" / "t-0004"
    (folder / "Frågor och svar").mkdir(parents=True)
    shutil.copyfile(sorted(PDFS.glob("*.pdf"))[0], folder / "beskrivning.pdf")
    (folder / "Frågor och svar" / "FoS 1.txt").write_text("Svar: nej.")
    (data_root / "catalogs").mkdir()
    catalog = {"name": "estim-default", "label": "x", "currency": "SEK", "hourly_rate": 650.0, "items": []}
    (data_root / "catalogs" / "estim-default.json").write_text(json.dumps(catalog))


class HeldOutEnd(FakeEngines):
    """Prospective runs finish; held-out runs end as `end` (finished or halted)."""

    def __init__(self, end: str) -> None:
        super().__init__(COSTS)
        self.end = end

    def _run(self, engine, variant, vroot, company_id, tender_id, run_id):  # type: ignore[no-untyped-def]
        one = super()._run(engine, variant, vroot, company_id, tender_id, run_id)
        if tender_id not in HELD:
            return one
        ok = self.end == freeze.FINISHED
        return dataclasses.replace(one, ok=ok, exit_code=0 if ok else 5, halted=self.end == freeze.HALTED)


def _exam(root: Path, monkeypatch: pytest.MonkeyPatch, deadlines: dict[str, str], end: str) -> list[str]:
    root.mkdir()
    monkeypatch.setenv("ANBUD_DATA_ROOT", str(root))
    _store(root, deadlines)
    register(root)
    lines: list[str] = []
    exam.run_both(
        "co-002",
        paid=True,
        budget_usd=500.0,
        runner=HeldOutEnd(end),
        now=NOW,
        local_now=LOCAL_NOW,
        say=lines.append,
    )
    assert lines[-1].startswith("paid exam: complete. One total over both splits"), lines[-1]
    return lines


@pytest.mark.parametrize("end", [freeze.FINISHED, freeze.HALTED])
def test_the_one_total_is_the_same_whether_the_prospective_tenders_are_past_their_deadline_or_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, end: str
) -> None:
    """Being past a deadline is a fact of prospective tenders only, so a total over both splits can't depend on it:
    if it did, it would hold a count of prospective runs, and the rest of the total minus it would be the held-out
    split's. Mutation: restoring the late count to `paid_summary` and `_say_paid` makes it fail."""
    past = _exam(tmp_path / "past", monkeypatch, PAST, end)
    still_open = _exam(tmp_path / "open", monkeypatch, OPEN, end)
    assert any("tenders are past their deadline" in x for x in past)
    assert not any("tenders are past their deadline" in x for x in still_open)
    assert past[-1] == still_open[-1]
    assert "deadline" not in past[-1] and "late" not in exam.paid_summary("co-002")


def test_whether_a_run_came_after_its_deadline_stays_on_its_freeze_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scoring needs it: a prospective run frozen after its tender's deadline is reported late and never scored."""
    _exam(tmp_path / "past", monkeypatch, PAST, freeze.FINISHED)
    ledger = freeze.ledger_file("co-002", sealed=True).read_text().splitlines()
    frozen = [r for r in map(json.loads, ledger) if r["kind"] == "freeze"]
    prospective = [r for r in frozen if r["exam"]["kind"] == "prospective"]
    assert prospective and all(r["exam"]["after_deadline"] is True for r in prospective)
