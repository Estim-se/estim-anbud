"""The exam on a synthetic sealed store: quarantine inside the exam, two inputs per tender, prep, the estimate, the old
engine's dry run, deadlines, and (with a fake runner) paid runs frozen into the sealed ledger within the budget.
Every tender, file and name here is invented."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import assert_no_fake_names
from test_baseline import ENGINE, LIB, fake_app, git
from test_intake_blind import notice_page

from anbud import cli, paths
from anbud.evals import exam, freeze
from anbud.evals import split_heldout as sh

NOW = datetime(2026, 12, 1, 9, 0, tzinfo=UTC)
LOCAL_NOW = datetime(2026, 12, 1, 10, 0)
PDFS = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "injection-tender" / "clean"
BUN = shutil.which("bun")
SECRET_WORDS = ("Fiktivbolaget", "Ändring", "FoS", "Annons", "Tilldelning", "Dnr", "t-000", "202100")


def tender_zip(deadline: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "Annons.html", notice_page("Fiktivbolaget AB", "202100-0001", "Dnr 2026/0101", "2026-10-01", deadline)
        )
        for pdf in sorted(PDFS.glob("*.pdf")):
            zf.writestr(f"Förfrågningsunderlag/{pdf.name}", pdf.read_bytes())
        zf.writestr("Frågor och svar/FoS 1.txt", "Fråga: om Fiktivbolaget. Svar: ja.")
        zf.writestr("Kompletteringar/Ändring 1.txt", "Ändring: 40 uttag i stället för 30.")
        zf.writestr("Tilldelningsbeslut.txt", "Tilldelning till Fiktivbolaget.")
    return buf.getvalue()


def sealed_store(data_root: Path) -> None:
    """Three prospective tenders, laid out as intake leaves them: two zips (one open, one past its deadline) and one
    folder delivery with no notice; and one held-out tender the prospective exam never touches."""
    statuses = {"t-0001": sh.HELDOUT, "t-0002": sh.PROSPECTIVE, "t-0003": sh.PROSPECTIVE, "t-0004": sh.PROSPECTIVE}
    sh.append_events(
        paths.ledger_path("co-002"),
        [sh._assign(t, s, "test", (), prospective=s == sh.PROSPECTIVE) for t, s in statuses.items()],
    )
    for t, deadline in (("t-0001", "2026-06-01"), ("t-0002", "2026-12-13 12:00"), ("t-0003", "2026-11-20")):
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


def files(folder: Path) -> list[str]:
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())


def test_the_exam_is_refused_outside_the_eval_lane(data_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sealed_store(data_root)
    assert cli.main(["eval", "prospective", "co-002"]) == cli.REFUSED
    assert not (data_root / "sealed").exists()


@pytest.mark.skipif(BUN is None, reason="bun isn't installed")
def test_a_dry_exam_quarantines_prepares_estimates_and_prints_aggregates_only(
    data_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sealed_store(data_root)
    held_out_before = files(data_root / "heldout")
    app = fake_app(tmp_path, ENGINE)
    for rel, text in LIB.items():
        (app / rel).parent.mkdir(parents=True, exist_ok=True)
        (app / rel).write_text(text)
    git(app, "add", "-A")
    git(app, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "lib")
    lines: list[str] = []
    report = exam.run_exam(
        "co-002", "prospective", app=app, app_ref="HEAD", now=NOW, local_now=LOCAL_NOW, say=lines.append
    )
    printed = "\n".join(lines)
    assert_no_fake_names(printed)
    for word in SECRET_WORDS:
        assert word not in printed  # aggregates only: no tender id, file name, buyer or notice value

    # Quarantine: three answer-key kinds held in each zip, one Q&A in the folder delivery; amendments go to Kalkyl.
    assert report.tenders == 3 and report.quarantined == 3 and report.held_tenders == 3
    assert report.held == {"qa": 3, "amendment": 2, "award": 2}
    assert report.amendments_to_kalkyl == 2 and report.same_input == 1
    for t in ("t-0002", "t-0003"):
        g = files(paths.dev_tender_dir("co-002", t, exam.variant_root("co-002", "prospective", "granskning")) / "inbox")
        k = files(paths.dev_tender_dir("co-002", t, exam.variant_root("co-002", "prospective", "kalkyl")) / "inbox")
        assert not any(("FoS" in f or "Ändring" in f or "Tilldelning" in f) for f in g)
        assert "Kompletteringar/Ändring 1.txt" in k and not any(("FoS" in f or "Tilldelning" in f) for f in k)
        assert len(files(exam.held_dir("co-002", t))) == 4  # three held files and their manifest
        record = json.loads((exam.exam_dir("co-002", "prospective") / "tenders" / f"{t}.json").read_text())
        assert record["variants"]["kalkyl"].startswith("with the amendments")
    assert files(data_root / "heldout") == held_out_before  # the sealed deliveries are read, never changed
    assert not (exam.exam_dir("co-002", "heldout")).exists()  # the held-out tender isn't part of this exam

    # Prep, the estimate and the dry runs.
    assert report.prepared == 3 and report.prep_failed == 0 and report.pages > 0
    assert set(report.estimate_usd) == {"granskning", "kalkyl", "baseline"} and report.estimate_unknown == 0
    assert 0 < report.estimate_total <= report.worst_case_usd
    assert report.baseline_runs == 5 and report.dry_runs_ok == {"baseline": 5}
    assert "dry runs: baseline 5 of 5 reached the first model call" in printed

    # Deadlines: one open in 12 days, one passed (warned), one unknown.
    assert "deadlines: 1 open, nearest open deadline in 12 days; 1 passed; 1 unknown" in printed
    assert "warning: 1 tenders are past their deadline" in printed

    # Everything the exam wrote is in sealed storage, apart from the exported app code.
    assert {p.name for p in data_root.iterdir()} <= {"sealed", "heldout", "co-002", "catalogs", "baseline"}
    assert files(data_root / "baseline") and all(f.startswith("app/") for f in files(data_root / "baseline"))

    # A second exam reuses the quarantine (same deliveries), and the held answers stay put.
    again = exam.run_exam(
        "co-002",
        "prospective",
        engines=("kalkyl",),
        app=app,
        app_ref="HEAD",
        now=NOW,
        local_now=LOCAL_NOW,
        say=lambda _: None,
    )
    assert again.held == report.held and again.estimate_usd.keys() == {"kalkyl"}


class FakeEngines(exam.Engines):
    """Prep writes a summary; runs write outputs and a record, cost what they're told, and never call a model."""

    def __init__(self, costs: dict[str, float | None]) -> None:
        self.costs = costs
        self.calls: list[tuple[str, str]] = []

    def prep(self, vroot: Path, company_id: str, tender_id: str, log_name: str) -> bool:
        prep = paths.dev_tender_dir(company_id, tender_id, vroot) / "prep"
        prep.mkdir(parents=True, exist_ok=True)
        (prep / "summary.json").write_text(json.dumps({"documents": 2, "pages_by_kind": {"text": 50}}))
        return True

    def granskning_estimate(self, vroot: Path, company_id: str, tender_id: str, log_name: str) -> int | None:
        return 10_000

    def kalkyl_estimate(self, vroot: Path, company_id: str, tender_id: str, log_name: str) -> float | None:
        return 1.0

    def _run(
        self, engine: str, variant: str, vroot: Path, company_id: str, tender_id: str, run_id: str
    ) -> exam.EngineRun:
        self.calls.append((engine, variant))
        out = vroot / "fake-runs" / run_id
        (out / "outputs").mkdir(parents=True)
        (out / "outputs" / "result.json").write_text("{}")
        (out / "record").mkdir()
        (out / "record" / "run.json").write_text(json.dumps({"code": {"harness_sha": "x"}}))
        settings = {"basis": "matched"} if engine == "baseline" else None
        return exam.EngineRun(
            engine, variant, run_id, 0, True, self.costs[engine], out / "outputs", out / "record", settings
        )

    def granskning(self, vroot: Path, company_id: str, tender_id: str, run_id: str) -> exam.EngineRun:
        return self._run("granskning", "granskning", vroot, company_id, tender_id, run_id)

    def kalkyl(self, vroot: Path, company_id: str, tender_id: str, run_id: str) -> exam.EngineRun:
        return self._run("kalkyl", "kalkyl", vroot, company_id, tender_id, run_id)

    def baseline(self, vroot, company_id, tender_id, variant, run_id, run_dir, *, dry_run):  # type: ignore[no-untyped-def]
        return self._run("baseline", variant, vroot, company_id, tender_id, run_id)


def test_a_paid_exam_freezes_each_run_with_its_input_and_stays_within_the_budget(data_root: Path) -> None:
    sealed_store(data_root)
    with pytest.raises(exam.ExamError, match="budget"):
        exam.run_exam(
            "co-002", "prospective", paid=True, budget_usd=1.0, runner=FakeEngines({}), now=NOW, say=lambda _: None
        )

    runner = FakeEngines({"granskning": 8.0, "kalkyl": None, "baseline": 2.0})
    lines: list[str] = []
    report = exam.run_exam(
        "co-002",
        "prospective",
        paid=True,
        budget_usd=40.0,
        runner=runner,
        now=NOW,
        local_now=LOCAL_NOW,
        say=lines.append,
    )
    # Per tender: Granskning ($20 stop), Kalkyl ($3 stop; no cost reported, so counted at its stop), the old engine
    # on each input ($10 stop). The estimate fits $40, but a run that could pass it by its hard stop isn't started:
    # the last tender's Granskning and old-engine runs.
    assert report.spent_usd <= 40.0 and report.runs_skipped_budget == 2
    assert sum(report.runs_ok.values()) == report.frozen == len(runner.calls)
    records = [json.loads(line) for line in freeze.ledger_file("co-002", sealed=True).read_text().splitlines()]
    assert len(records) == report.frozen and all(r["kind"] == "freeze" for r in records)
    for r in records:
        assert r["exam"]["variant"] in exam.VARIANTS and r["exam"]["kind"] == "prospective"
        assert r["exam"]["variant"] == "kalkyl" or r["exam"]["engine"] != "kalkyl"
        assert r["input_manifest_sha256"]  # hashed from the input the run read, not a dev folder
    after = {r["tender"]: r["exam"]["after_deadline"] for r in records}
    assert after.get("co-002/t-0003") is True and after.get("co-002/t-0002", False) is False
    assert not freeze.ledger_file("co-002").exists()  # nothing in the dev ledger
    assert "frozen runs came after their tender's deadline" in "\n".join(lines)
