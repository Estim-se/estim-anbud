"""The exam on a synthetic sealed store: quarantine inside the exam, two inputs per tender, prep and why it stopped, the
estimate and the prefix sizes, the old engine's dry run, deadlines, and (with a fake runner) paid runs frozen into the
sealed ledger within the budget. Every tender, file and name here is invented."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tomllib
import zipfile
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import assert_no_fake_names
from test_baseline import ENGINE, LIB, fake_app, git
from test_intake_blind import notice_page

from anbud import cli, intake, paths
from anbud.config import CONFIG_DIR
from anbud.evals import baseline, exam, freeze
from anbud.evals import split_heldout as sh
from anbud.granskning import command as cli_command

NOW = datetime(2026, 12, 1, 9, 0, tzinfo=UTC)
LOCAL_NOW = datetime(2026, 12, 1, 10, 0)
PDFS = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "injection-tender" / "clean"
BUN = shutil.which("bun")
SECRET_WORDS = ("Fiktivbolaget", "Ändring", "FoS", "Annons", "Tilldelning", "Dnr", "t-000", "202100")


def tender_zip(deadline: str, *, marked: bool = False) -> bytes:
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
        zf.writestr("Ritningar/E-10.dwg", b"AC1027" + bytes(64))  # prep skips drawings and counts them
        if marked:  # an invented classification marking: prep stops the whole tender on it
            zf.writestr("Förfrågningsunderlag/PM skydd.txt", "HEMLIG\n\nInventerat innehåll.\n")
    return buf.getvalue()


def sealed_store(data_root: Path, marked: tuple[str, ...] = ()) -> None:
    """Three prospective tenders, laid out as intake leaves them: two zips (one open, one past its deadline) and one
    folder delivery with no notice; and one held-out tender the prospective exam never touches. A zip tender in
    `marked` holds a document with a security marking."""
    statuses = {"t-0001": sh.HELDOUT, "t-0002": sh.PROSPECTIVE, "t-0003": sh.PROSPECTIVE, "t-0004": sh.PROSPECTIVE}
    sh.append_events(
        paths.ledger_path("co-002"),
        [sh._assign(t, s, "test", (), prospective=s == sh.PROSPECTIVE) for t, s in statuses.items()],
    )
    for t, deadline in (("t-0001", "2026-06-01"), ("t-0002", "2026-12-13 12:00"), ("t-0003", "2026-11-20")):
        folder = data_root / "heldout" / "co-002" / t
        data = tender_zip(deadline, marked=t in marked)
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
    assert report.skipped_by_type == {"dwg": 2} and "skipped by type: dwg 2" in printed
    assert set(report.estimate_usd) == {"granskning", "kalkyl", "baseline"} and report.estimate_unknown == 0
    assert 0 < report.estimate_total <= report.worst_case_usd
    assert report.baseline_runs == 5 and report.dry_runs_ok == {"baseline": 5}
    assert "dry runs, both splits: baseline 5 of 5 reached the first model call" in printed
    assert report.prefix_tokens["runs"] == 3 and 0 < report.prefix_tokens["median"] <= report.prefix_tokens["max"]
    assert report.prefix_tokens["over_context"] == report.prefix_tokens["over_large"] == 0
    assert "granskning prefix after the selection, before the prefix budget (dry-run estimate" in printed

    # One total over both splits; only this split has been examined, with three prepared tenders.
    assert report.combined["tenders"] == 3 and report.combined["by_split"] == {"prospective": 3}
    assert report.combined["selection"]["tenders"] == 3 and report.combined["selection"]["would_stop"] == 0
    assert "both splits, one total over 3 prepared tenders: " in printed
    assert "prefix budget 450,000 tokens: 0 of 3 tenders would stop" in printed

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

    def __init__(
        self,
        costs: dict[str, float | None],
        stops: dict[str, str] | None = None,
        tokens: dict[str, int] | None = None,
        selections: dict[str, dict] | None = None,
        prefix_stops: bool = True,
        ends: dict[str, str] | None = None,
        bare: frozenset[str] = frozenset(),
        nothing: frozenset[str] = frozenset(),
        budgets: dict[str, dict] | None = None,
        api: list[str | None] | None = None,
        failures: dict[tuple[str, str], dict[str, int]] | None = None,
        codes: dict[tuple[str, str], int] | None = None,
        tiles: dict[str, int] | None = None,
        highs: dict[str, float] | None = None,
        pages: dict[str, int] | None = None,
    ) -> None:
        self.tiles = tiles or {}  # tender -> the tiles its Granskning dry run names; none when not given
        self.highs = highs or {}  # tender -> its Kalkyl dry run's high estimate; $1 when not given
        self.pages = pages or {}  # tender -> its prep's text pages; 50 when not given
        self.api = list(api or [])  # what each $0 API check finds, in order; it passes once these run out
        self.api_checks = 0
        self.failures = failures or {}  # (tender, engine) -> its calls failed for good, by kind
        self.codes = codes or {}  # (tender, engine) -> the exit code its run ends with, as a failed run
        self.prepped: list[str] = []
        self.ends = ends or {}  # how each engine's runs end; finished unless given
        self.budgets = budgets or {}  # tender -> the `budget` section its Granskning run's summary reports
        self.bare = bare  # engines whose runs write no outputs folder
        self.nothing = nothing  # engines whose runs leave no folder at all
        self.costs = costs
        self.stops = stops or {}
        self.tokens = tokens or {}
        self.selections = selections or {}
        self.prefix_stops = prefix_stops  # what the $0 check finds for a tender over the prefix budget
        self.checked: list[str] = []
        self.calls: list[tuple[str, str]] = []

    def api_check(self) -> str | None:
        self.api_checks += 1
        return self.api.pop(0) if self.api else None

    def launch_sha(self) -> str | None:
        return "f" * 40

    def gate(self, launch_sha: str | None) -> str | None:  # the tree is the fake's own: only the API check is scripted
        kind = self.api_check()
        return None if kind is None else f"the API check failed ({kind})"

    def granskning_stop_check(self, vroot: Path, company_id: str, tender_id: str, run_id: str) -> bool:
        self.checked.append(tender_id)
        return self.prefix_stops

    def selection_plan(self, vroot: Path, company_id: str, tender_id: str) -> dict | None:
        return self.selections.get(tender_id)

    def prep(self, vroot: Path, company_id: str, tender_id: str, log_name: str) -> str | None:
        self.prepped.append(tender_id)
        if tender_id in self.stops:
            return self.stops[tender_id]
        prep = paths.dev_tender_dir(company_id, tender_id, vroot) / "prep"
        prep.mkdir(parents=True, exist_ok=True)
        pages = self.pages.get(tender_id, 50)
        (prep / "summary.json").write_text(json.dumps({"documents": 2, "pages_by_kind": {"text": pages}}))
        return None

    def granskning_estimate(self, vroot: Path, company_id: str, tender_id: str, log_name: str) -> int | None:
        return self.tokens.get(tender_id, 10_000)

    def kalkyl_estimate(self, vroot: Path, company_id: str, tender_id: str, log_name: str) -> float | None:
        return self.highs.get(tender_id, 1.0)

    def granskning_tiles(self, tender_id: str) -> int | None:
        return self.tiles.get(tender_id)

    def _run(
        self, engine: str, variant: str, vroot: Path, company_id: str, tender_id: str, run_id: str
    ) -> exam.EngineRun:
        self.calls.append((engine, variant))
        end = self.ends.get(engine, freeze.FINISHED)
        if engine in self.nothing:
            return exam.EngineRun(engine, variant, run_id, None, False, 0.0)
        out = vroot / "fake-runs" / run_id
        if engine not in self.bare:
            (out / "outputs").mkdir(parents=True)
            (out / "outputs" / "result.json").write_text("{}")
        (out / "record").mkdir(parents=True)
        (out / "record" / "run.json").write_text(json.dumps({"code": {"harness_sha": "x"}}))
        settings = {"basis": "matched"} if engine == "baseline" else None
        code = self.codes.get((tender_id, engine))
        ok = end == freeze.FINISHED and code is None
        return exam.EngineRun(
            engine,
            variant,
            run_id,
            0 if ok else code or 1,
            ok,
            self.costs[engine],
            out / "outputs",
            out / "record",
            settings,
            halted=end == freeze.HALTED,
            budget=self.budgets.get(tender_id) if engine == "granskning" else None,
            failures=self.failures.get((tender_id, engine)),
        )

    def granskning(self, vroot: Path, company_id: str, tender_id: str, run_id: str) -> exam.EngineRun:
        return self._run("granskning", "granskning", vroot, company_id, tender_id, run_id)

    def kalkyl(self, vroot: Path, company_id: str, tender_id: str, run_id: str) -> exam.EngineRun:
        return self._run("kalkyl", "kalkyl", vroot, company_id, tender_id, run_id)

    def baseline(self, vroot, company_id, tender_id, variant, run_id, run_dir, *, dry_run):  # type: ignore[no-untyped-def]
        return self._run("baseline", variant, vroot, company_id, tender_id, run_id)


def register(data_root: Path, text: str = "Scoring plan v1.\n") -> dict:
    plan = data_root / "predictions" / "co-002" / "scoring-plan-v1.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(text)
    return freeze.register_plan("co-002", plan)


def test_a_scoring_plan_is_registered_once_per_version_and_must_stay_unchanged(data_root: Path) -> None:
    assert freeze.current_plan("co-002") is None
    first = register(data_root)
    assert first["new"] and first["line_number"] == 1
    assert register(data_root)["new"] is False  # the same bytes: a no-op
    assert freeze.current_plan("co-002")["sha256"] == first["sha256"]
    (data_root / "predictions" / "co-002" / "scoring-plan-v1.md").write_text("Edited.\n")
    with pytest.raises(freeze.FreezeError, match="changed or is gone"):
        freeze.current_plan("co-002")
    second = register(data_root, "Scoring plan v1, edited.\n")
    assert second["new"] and second["line_number"] == 2 and freeze.current_plan("co-002")["line_number"] == 2
    assert freeze.verify(freeze.ledger_file("co-002")).kinds == {"preregistration": 2}
    elsewhere = data_root / "plan.md"
    elsewhere.write_text("x")
    with pytest.raises(freeze.FreezeError, match="directly in predictions"):
        freeze.register_plan("co-002", elsewhere)
    named = data_root / "predictions" / "co-002" / "Plan Fiktivbolaget.md"
    named.write_text("x")
    with pytest.raises(freeze.FreezeError, match="lower-case"):
        freeze.register_plan("co-002", named)


def test_a_paid_exam_freezes_each_run_with_its_input_and_stays_within_the_budget(data_root: Path) -> None:
    sealed_store(data_root)
    with pytest.raises(exam.ExamError, match="scoring plan"):
        exam.run_exam(
            "co-002", "prospective", paid=True, budget_usd=40.0, runner=FakeEngines({}), now=NOW, say=lambda _: None
        )
    assert not (data_root / "sealed").exists()  # refused before anything was unpacked
    plan = register(data_root)
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
    assert report.prefix_tokens == {
        "runs": 3,
        "median": 10_000,
        "max": 10_000,
        "over_context": 0,
        "over_large": 0,
        "context": 1_000_000,
        "large": 200_000,
    }
    assert sum(report.runs_ok.values()) == report.frozen == len(runner.calls)
    lines_on_record = [json.loads(x) for x in freeze.ledger_file("co-002", sealed=True).read_text().splitlines()]
    # Each run is put on record before it starts, then frozen: a started line, then its freeze line.
    assert [r["kind"] for r in lines_on_record] == ["started", "freeze"] * report.frozen
    assert all(a["run_id"] == b["run_id"] for a, b in zip(lines_on_record[::2], lines_on_record[1::2], strict=True))
    records = [r for r in lines_on_record if r["kind"] == "freeze"]
    assert sum(r["exam"]["charged_usd"] for r in records) == pytest.approx(report.spent_usd)
    assert report.combined["paid"]["charged_usd"] == pytest.approx(report.spent_usd)
    for r in records:
        assert r["exam"]["variant"] in exam.VARIANTS and r["exam"]["kind"] == "prospective"
        assert r["exam"]["plan"] == {"file": "scoring-plan-v1.md", "sha256": plan["sha256"], "line": 1}
        assert r["exam"]["variant"] == "kalkyl" or r["exam"]["engine"] != "kalkyl"
        assert r["input_manifest_sha256"]  # hashed from the input the run read, not a dev folder
    after = {r["tender"]: r["exam"]["after_deadline"] for r in records}
    assert after.get("co-002/t-0003") is True and after.get("co-002/t-0002", False) is False
    plans = freeze.verify(freeze.ledger_file("co-002")).kinds
    assert plans == {"preregistration": 1}  # the plan only: no run is frozen in the plain ledger
    assert report.runs_after_deadline > 0
    assert not [line for line in lines if line.startswith("runs")]  # a pass prints no figure of its runs (run_both)


def _sealed_lines() -> list[dict]:
    return [json.loads(line) for line in freeze.ledger_file("co-002", sealed=True).read_text().splitlines()]


def _freezes() -> list[dict]:
    return [r for r in _sealed_lines() if r["kind"] == "freeze"]


def _paid(runner: FakeEngines, lines: list[str], budget: float = 200.0) -> exam.Report:
    return exam.run_exam(
        "co-002",
        "prospective",
        paid=True,
        budget_usd=budget,
        runner=runner,
        now=NOW,
        local_now=LOCAL_NOW,
        say=lines.append,
    )


def test_a_run_that_halts_or_fails_is_frozen_with_its_status_and_never_counted_finished(data_root: Path) -> None:
    sealed_store(data_root)
    register(data_root)
    runner = FakeEngines(
        {"granskning": 8.0, "kalkyl": 1.0, "baseline": 2.0},
        ends={"granskning": freeze.HALTED, "kalkyl": freeze.FAILED},
        bare=frozenset({"kalkyl"}),
    )
    lines: list[str] = []
    report = _paid(runner, lines)
    everything = _sealed_lines()
    records = [r for r in everything if r["kind"] == "freeze"]
    finished = report.runs_ok["baseline"]
    assert Counter((r["exam"]["engine"], r["status"]) for r in records) == {
        ("granskning", "halted"): 3,
        ("kalkyl", "failed"): 3,
        ("baseline", "finished"): finished,
    }
    assert report.runs_halted == {"granskning": 3} and report.runs_failed == {"kalkyl": 3}
    assert report.runs_ok.keys() == {"baseline"} and report.frozen == len(records) == len(runner.calls)
    assert report.frozen_by_status == {"failed": 3, "finished": finished, "halted": 3} and report.not_frozen == 0
    kalkyl = [r for r in records if r["exam"]["engine"] == "kalkyl"]
    assert all(r["outputs_dir"] is None and r["outputs_sha256"] == {} for r in kalkyl)  # nothing written, still frozen
    late = sum(1 for r in records if r["exam"]["after_deadline"] and r["status"] == "finished")
    assert 0 < late == report.runs_after_deadline  # only a finished run counts as a late prediction
    assert not [line for line in lines if line.startswith("runs")]  # its figures print once, in run_both's total
    assert report.runs_skipped_budget == 0 and report.owed == 0
    exams = list((exam.exam_dir("co-002", "prospective") / "exams").glob("*.json"))
    runs = json.loads(exams[0].read_text())["runs"]
    assert Counter(r["status"] for r in runs) == {"halted": 3, "failed": 3, "finished": finished}
    # Under scoring plan v3 neither the halted review nor the failed calc is ever a scored prediction: each tender
    # has no prediction for those engines, while its finished old-engine runs are scored.
    cutoff = datetime(2027, 1, 1, tzinfo=UTC)
    for tender in sorted({r["tender"] for r in records}):
        assert freeze.scored_prediction(everything, tender, "granskning", "granskning", cutoff) is None
        assert freeze.scored_prediction(everything, tender, "kalkyl", "kalkyl", cutoff) is None
        inputs = {r["exam"]["variant"] for r in records if r["tender"] == tender and r["exam"]["engine"] == "baseline"}
        assert inputs and all(freeze.scored_prediction(everything, tender, "baseline", v, cutoff) for v in inputs)


def test_a_run_that_leaves_no_folder_is_counted_and_not_frozen(data_root: Path) -> None:
    sealed_store(data_root)
    register(data_root)
    runner = FakeEngines({"granskning": 8.0, "kalkyl": 1.0, "baseline": 2.0}, nothing=frozenset({"baseline"}))
    lines: list[str] = []
    report = _paid(runner, lines)
    assert {r["exam"]["engine"] for r in _freezes()} == {"granskning", "kalkyl"}
    assert report.not_frozen == report.runs_failed["baseline"] > 0
    # Each one is still on record as an attempt, started and never frozen: no prediction, and never run again.
    started = [r for r in _sealed_lines() if r["kind"] == "started" and r["exam"]["engine"] == "baseline"]
    assert len(started) == report.not_frozen
    assert report.combined["paid"]["ended"][exam.NEVER_FROZEN] == {"baseline": report.not_frozen}


@pytest.mark.parametrize(
    ("engine", "code", "summary", "status"),
    [
        ("granskning", 0, {}, "finished"),
        ("granskning", cli_command.RUN_HALTED, {}, "halted"),
        ("granskning", 1, {}, "failed"),
        ("granskning", cli_command.FINISHED_NOT_FROZEN, {}, "finished"),  # its own freeze failed; the sealed one won't
        ("kalkyl", 0, {"frozen": True, "halted": 0}, "finished"),
        ("kalkyl", 0, {"frozen": False, "halted": 2}, "halted"),  # a limit stopped it: its calc is incomplete
        ("kalkyl", 0, {"frozen": False, "halted": 0}, "failed"),
        ("kalkyl", 3, {}, "failed"),
        ("kalkyl", cli_command.FINISHED_NOT_FROZEN, {"frozen": True, "halted": 0}, "finished"),
        ("kalkyl", cli_command.FINISHED_NOT_FROZEN, {"frozen": False, "halted": 0}, "failed"),
    ],
)
def test_each_engine_s_exit_says_whether_it_finished_halted_or_failed(
    engine: str, code: int, summary: dict, status: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = exam.Engines(tmp_path / "logs", tmp_path, "HEAD", tmp_path)
    stdout = "\n".join(f"{k}: {v}" for k, v in {"cost_usd": 1.5, **summary}.items())  # as `anbud kalkyl` prints
    monkeypatch.setattr(runner, "_anbud", lambda vroot, args, log: subprocess.CompletedProcess(args, code, stdout, ""))
    one = getattr(runner, engine)(tmp_path, "co-002", "t-0002", "r1")
    assert one.status == status and exam.RUN_HALTED == cli_command.RUN_HALTED
    assert exam.FINISHED_NOT_FROZEN == cli_command.FINISHED_NOT_FROZEN
    assert (
        set(exam.BASELINE_HALTS) == {"hard_stop", "stopped", "timeout"} and "hard_stop" in baseline.EXIT_CODES.values()
    )


def test_prep_stops_are_counted_by_category_and_prefix_sizes_summarised(data_root: Path) -> None:
    sealed_store(data_root)
    runner = FakeEngines(
        {"granskning": 0.0, "kalkyl": 0.0, "baseline": 0.0},
        stops={"t-0003": "security_marking"},
        tokens={"t-0002": 250_000, "t-0004": 1_200_000},
    )
    lines: list[str] = []
    report = exam.run_exam("co-002", "prospective", runner=runner, now=NOW, local_now=LOCAL_NOW, say=lines.append)
    printed = "\n".join(lines)
    assert report.prepared == 2 and report.prep_failed == 1 and report.prep_stops == {"security_marking": 1}
    assert "; prep: 2 prepared, 1 stopped (security_marking 1; 1 of them kept, not read again by a later pass)" in (
        printed
    )
    assert report.prefix_tokens["median"] == 725_000 and report.prefix_tokens["max"] == 1_200_000
    assert report.prefix_tokens["over_context"] == 1 and report.prefix_tokens["over_large"] == 2
    assert (
        "granskning prefix after the selection, before the prefix budget (dry-run estimate, before page cards), over "
        "the 2 tenders it runs on: median 725,000 tokens, max 1,200,000; 1 over the context (1,000,000), 2 over 200,000"
    ) in printed
    stopped = paths.dev_tender_dir("co-002", "t-0003", exam.variant_root("co-002", "prospective", "kalkyl"))
    assert not (stopped / "prep").exists()  # a tender whose first input stopped isn't prepared a second time


@pytest.mark.parametrize(
    ("code", "stderr", "category"),
    [
        (0, "", None),
        (3, "anbud prep: refused: a held-out tender\n", "refused"),
        (
            3,
            "anbud prep: stopped: 2 security marking(s) (hemlig), the first in document x, block y.\n",
            "security_marking",
        ),
        (3, "anbud prep: stopped: two files share a block-id prefix; block ids would collide\n", "id_collision"),
        (
            3,
            "anbud prep: stopped: the inbox, manifest or output folder is in held-out or sealed storage",
            "sealed_storage",
        ),
        (3, "anbud prep: stopped: the inbox doesn't match its intake manifest (1 unexpected, 0 missing)\n", "manifest"),
        (3, "anbud prep: stopped: the tender has no inbox folder, or it is a link\n", "manifest"),
        (3, "anbud prep: stopped: a file in the inbox can't be read\n", "unreadable_file"),
        (3, "anbud prep: stopped: MemoryError while reading the tender\n", "read_error"),
        (3, "anbud prep: stopped: a write outside the company's folder\n", "other_stop"),
        (1, "Traceback (most recent call last):\nKeyError: 'x'\n", "crash"),
        (-9, "", "crash"),
    ],
)
def test_a_prep_stop_is_categorised_from_its_one_stderr_line(code: int, stderr: str, category: str | None) -> None:
    assert exam.prep_stop(code, stderr) == category


def test_every_prep_stop_fragment_is_one_prep_still_writes() -> None:
    """The categories match prep's own messages; a reworded message fails here, not silently as `other_stop`."""
    src = Path(__file__).resolve().parents[1] / "src" / "anbud"
    text = "".join(p.read_text() for p in [*sorted((src / "prep").glob("*.py")), src / "cli.py", src / "store.py"])
    for fragment, _ in exam.PREP_STOPS:
        assert fragment in text


def _dev_tender(vroot: Path, tender_id: str, inbox_files: dict[str, bytes], *, dev: bool = True) -> Path:
    tender = paths.dev_tender_dir("co-002", tender_id, vroot)
    for rel, data in inbox_files.items():
        (tender / "inbox" / rel).parent.mkdir(parents=True, exist_ok=True)
        (tender / "inbox" / rel).write_bytes(data)
    (tender / "inbox.sha256").write_text(intake._manifest(tender / "inbox"))
    if dev:
        exam._ensure_dev(vroot, "co-002", tender_id)
    return tender


def test_real_prep_stops_on_synthetic_files_get_their_category(data_root: Path, tmp_path: Path) -> None:
    """`anbud prep` in a child process, as the exam runs it, on invented files that make it stop."""
    vroot = data_root / "variant"
    runner = exam.Engines(data_root / "logs", tmp_path, "HEAD", None)
    text = b"Teknisk beskrivning.\n\nForty sockets in the hall.\n"
    _dev_tender(vroot, "t-0001", {"beskrivning.txt": text})
    _dev_tender(vroot, "t-0002", {"beskrivning.txt": text, "pm.txt": b"HEMLIG\n\nNot for the bidders.\n"})
    late = _dev_tender(vroot, "t-0003", {"beskrivning.txt": text})
    (late / "inbox" / "added-after-intake.txt").write_bytes(b"A file the manifest doesn't list.\n")
    _dev_tender(vroot, "t-0005", {"beskrivning.txt": text}, dev=False)  # not a dev tender in this data root
    expected = {"t-0001": None, "t-0002": "security_marking", "t-0003": "manifest", "t-0005": "refused"}
    if os.geteuid() != 0:  # root reads a file without read permission
        locked = _dev_tender(vroot, "t-0004", {"beskrivning.txt": text, "låst.txt": b"Locked.\n"})
        (locked / "inbox" / "låst.txt").chmod(0)
        expected["t-0004"] = "unreadable_file"
    try:
        got = {t: runner.prep(vroot, "co-002", t, f"prep-{t}.log") for t in expected}
    finally:
        for path in vroot.rglob("låst.txt"):
            path.chmod(0o600)
    assert got == expected
    assert (paths.dev_tender_dir("co-002", "t-0001", vroot) / "prep" / "summary.json").is_file()
    assert sorted(p.name for p in (data_root / "logs").iterdir()) == sorted(f"prep-{t}.log" for t in expected)


class PrepOnly(exam.Engines):
    """Real prep, as the exam runs it; estimates without a child process, and no old engine."""

    def __init__(self, logs: Path, app: Path) -> None:
        super().__init__(logs, app, "HEAD", None)
        self.prepped: list[str] = []

    def prep(self, vroot: Path, company_id: str, tender_id: str, log_name: str) -> str | None:
        self.prepped.append(tender_id)
        return super().prep(vroot, company_id, tender_id, log_name)

    def granskning_estimate(self, vroot: Path, company_id: str, tender_id: str, log_name: str) -> int | None:
        return 10_000


def test_a_security_marking_stop_is_kept_and_the_tender_never_read_again(
    data_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed_store(data_root, marked=("t-0002",))
    runner = PrepOnly(data_root / "logs", tmp_path)

    def exam_pass(say: Callable[[str], None]) -> exam.Report:
        return exam.run_exam(
            "co-002", "prospective", engines=("granskning",), runner=runner, now=NOW, local_now=LOCAL_NOW, say=say
        )

    first = exam_pass(lambda _: None)
    assert first.prepared == 2 and first.prep_stops == {"security_marking": 1} and first.prep_stops_kept == 0
    assert runner.prepped.count("t-0002") == 1  # stopped on its first input; the second isn't prepared
    record = json.loads((exam.exam_dir("co-002", "prospective") / "tenders" / "t-0002.json").read_text())
    assert record["prep_stop"] == "security_marking"

    quarantined: list[str] = []
    real_quarantine = exam.quarantine

    def watched(company_id: str, kind: str, tender_id: str, root: Path | None = None) -> dict:
        quarantined.append(tender_id)
        return real_quarantine(company_id, kind, tender_id, root)

    monkeypatch.setattr(exam, "quarantine", watched)
    runner.prepped.clear()
    lines: list[str] = []
    again = exam_pass(lines.append)
    assert "t-0002" not in quarantined and "t-0002" not in runner.prepped  # neither unpacked nor read again
    assert sorted(runner.prepped) == ["t-0003", "t-0003", "t-0004", "t-0004"]
    assert again.prepared == 2 and again.prep_stops == {"security_marking": 1} and again.prep_stops_kept == 1
    assert again.quarantined == first.quarantined == 3 and again.held == first.held
    assert "1 stopped (security_marking 1; 1 of them kept, not read again by a later pass)" in "\n".join(lines)


def test_any_other_prep_stop_is_retried_on_the_next_pass(data_root: Path) -> None:
    sealed_store(data_root)
    runner = FakeEngines({"granskning": 0.0, "kalkyl": 0.0, "baseline": 0.0}, stops={"t-0003": "manifest"})
    for _ in range(2):
        report = exam.run_exam(
            "co-002", "prospective", engines=("granskning",), runner=runner, now=NOW, say=lambda _: None
        )
        assert report.prep_stops == {"manifest": 1} and report.prep_stops_kept == 0
    record = json.loads((exam.exam_dir("co-002", "prospective") / "tenders" / "t-0003.json").read_text())
    assert "prep_stop" not in record


def test_no_figure_for_a_group_of_fewer_than_two_tenders(data_root: Path) -> None:
    """The held-out split here is one tender. Its pass prints its tender count only: every count, size and estimate is
    one total over both splits, printed once, after both splits (test_exam_counts_both)."""
    sealed_store(data_root)
    runner = FakeEngines({"granskning": 0.0, "kalkyl": 0.0, "baseline": 0.0})
    lines: list[str] = []
    reports = exam.run_both("co-002", runner=runner, now=NOW, local_now=LOCAL_NOW, say=lines.append)
    held_out = lines.index("anbud eval heldout: 1 tenders; counts are over both splits")
    assert lines[held_out + 1].startswith("both splits, 4 tenders: answer keys held in ")  # nothing of its own between
    assert lines[held_out + 1].endswith("; prep: 4 prepared, 0 stopped")
    assert lines[held_out + 2] == "dry runs, both splits: baseline 7 of 7 reached the first model call"
    assert [x for x in lines if x.startswith("both splits, one total")] == [
        "both splits, one total over 4 prepared tenders: 8 documents (skipped by type: none), 200 pages (granskning "
        "input)"
    ]
    # Per tender: Granskning 10,000 tokens ($0.975), Kalkyl $1.50, the old engine 50 pages per input ($1.65); four
    # tenders, seven old-engine inputs (the folder delivery has one).
    assert lines[-1] == (
        "estimate: $21.45 (baseline $11.55, granskning $3.90, kalkyl $6.00; 7 old-engine runs); "
        "worst case $162.00 (every hard stop: baseline $70.00, granskning $80.00, kalkyl $12.00)"
    )
    # The estimate id is a hash of that one total, the same from either split's report.
    assert len({exam.estimate_digest(r) for r in reports}) == 1 and exam.estimate_digest(reports[-1]) is not None


def test_the_cli_prints_an_estimate_id_only_for_a_total_of_two_or_more(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "held_out_refusal", lambda: None)
    one = exam.Report(kind="heldout", tenders=1, combined={"tenders": 1, "estimate_usd": {"kalkyl": 3.0}})
    monkeypatch.setattr(exam, "run_both", lambda *a, **kw: [exam.Report(kind="prospective"), one])
    assert cli.main(["eval", "exam", "co-002"]) == 0
    assert "estimate id" not in capsys.readouterr().out
    one.combined = {"tenders": 2, "estimate_usd": {"kalkyl": 3.0}, "worst_usd": 6.0}
    assert cli.main(["eval", "exam", "co-002"]) == 0
    assert f"estimate id: {exam.estimate_digest(one)}" in capsys.readouterr().out
    one.estimate_usd = {"kalkyl": 1.0}  # the split's own estimate doesn't enter the id
    assert cli.main(["eval", "exam", "co-002"]) == 0
    assert f"estimate id: {exam.estimate_digest(one)}" in capsys.readouterr().out
    assert exam.estimate_digest(one) == exam.estimate_digest(exam.Report(kind="prospective", combined=one.combined))


def test_the_selection_and_the_prefix_budget_are_one_total_over_the_tenders(data_root: Path) -> None:
    sealed_store(data_root)
    sizes = {
        "t-0002": (10, 1, 400, 300, True, 100_000, 2),
        "t-0003": (10, 3, 600, 100, False, 900_000, 0),
        "t-0004": (20, 0, 1000, 600, True, 150_000, 5),
    }
    keys = ("documents", "unknown_documents", "pages", "pages_excluded", "p1_fits", "planned_tokens", "search_only")
    selections = {t: {**dict(zip(keys, v, strict=True)), "budget_tokens": 450_000} for t, v in sizes.items()}
    runner = FakeEngines({"granskning": 0.0, "kalkyl": 0.0, "baseline": 0.0}, selections=selections)
    lines: list[str] = []
    report = exam.run_exam("co-002", "prospective", runner=runner, now=NOW, local_now=LOCAL_NOW, say=lines.append)
    assert (
        "selection: 50.0% of pages left out as other trades', 10.0% of documents unknown; selected pages median 400, "
        "max 500"
    ) in lines
    assert (
        "prefix budget 450,000 tokens: 1 of 3 tenders would stop (fixed part and every document's outline over the "
        "budget, 1 of them "
        "checked at $0 to stop before any model call and counted at $0); planned prefix median 125,000 tokens, max "
        "150,000 over the 2 that fit; 7 documents search-only"
    ) in lines
    # Granskning's basis is the planned prefix: 100,000 and 150,000 tokens. The tender that would stop was checked
    # at $0 and is counted at $0, in the estimate and in the worst case: its run is never started.
    assert runner.checked == ["t-0003"]
    assert report.combined["estimate_usd"]["granskning"] == pytest.approx(9.75 + 14.625, abs=0.01)
    assert report.combined["worst_usd"] == 20 + 20 + 3 * 3 + 5 * 10


KEYS = ("documents", "unknown_documents", "pages", "pages_excluded", "p1_fits", "planned_tokens", "search_only")


def _selections(sizes: dict[str, tuple]) -> dict[str, dict]:
    return {t: {**dict(zip(KEYS, v, strict=True)), "budget_tokens": 450_000} for t, v in sizes.items()}


def test_no_printed_figure_depends_on_the_tier_of_a_single_stopping_tender(data_root: Path) -> None:
    """One tender stops on the prefix budget, and the others are of mixed tiers. Whatever the stopper's tier from its
    dry run, every printed line is the same: its tier, its prefix and its caps enter no figure."""
    sealed_store(data_root)
    costs = {"granskning": 0.0, "kalkyl": 0.0, "baseline": 0.0}
    exam.run_exam("co-002", "heldout", runner=FakeEngines(costs), now=NOW, local_now=LOCAL_NOW, say=lambda _: None)
    selections = _selections(
        {
            "t-0002": (10, 1, 400, 300, True, 100_000, 2),
            "t-0003": (10, 3, 600, 100, False, 900_000, 0),  # stops: priority 1 over the budget
            "t-0004": (20, 0, 1000, 600, True, 150_000, 5),
        }
    )
    printed: list[list[str]] = []
    ids: list[str | None] = []
    for second, stopper in enumerate((100_000, 250_000, 1_200_000), start=1):  # tier S, M and above L
        tokens = {"t-0002": 100_000, "t-0003": stopper, "t-0004": 250_000}  # S and M among the others
        runner = FakeEngines(costs, tokens=tokens, selections=selections)
        lines: list[str] = []
        now = NOW.replace(second=second)  # a run id of its own per pass; the same day for the deadlines
        report = exam.run_exam("co-002", "prospective", runner=runner, now=now, local_now=LOCAL_NOW, say=lines.append)
        assert runner.checked == ["t-0003"] and report.combined["prefix_stopped"] == 1
        printed.append(lines)
        ids.append(exam.estimate_digest(report))
    assert printed[0] == printed[1] == printed[2] and ids[0] == ids[1] == ids[2]
    lines = printed[0]
    assert (
        "hard-stop tiers, tenders per tier from the dry runs, over the tenders each engine runs on: granskning S 2, "
        "M 1, L 0; kalkyl S 4, M 0, L 0; 1 stopping on the prefix budget not counted"
    ) in lines
    assert any(
        line.startswith("granskning prefix after the selection") and "over the 3 tenders it runs on" in line
        for line in lines
    )
    # The worst case per engine is the caps the paid gate uses: Granskning S 20 + S 20 + M 30 and the stopper $0,
    # Kalkyl 4 x S $3, the old engine 7 inputs x $10.
    assert lines[-1].endswith("(every hard stop: baseline $70.00, granskning $70.00, kalkyl $12.00)")


def test_an_engine_on_a_single_tender_prints_totals_only(data_root: Path) -> None:
    """Two of three tenders stop on the prefix budget, so Granskning runs on one: no Granskning prefix, tier or
    planned-prefix figure, no tiers that would give its cap back from the worst case, and no per-engine split."""
    sealed_store(data_root)
    selections = _selections(
        {
            "t-0002": (10, 1, 400, 300, False, 900_000, 0),
            "t-0003": (10, 3, 600, 100, False, 900_000, 0),
            "t-0004": (20, 0, 1000, 600, True, 150_000, 5),
        }
    )
    printed: list[list[str]] = []
    for second, alone in enumerate((100_000, 400_000), start=1):  # the one Granskning run's tier: S or L
        runner = FakeEngines(
            {"granskning": 0.0, "kalkyl": 0.0, "baseline": 0.0}, tokens={"t-0004": alone}, selections=selections
        )
        lines: list[str] = []
        now = NOW.replace(second=second)
        report = exam.run_exam("co-002", "prospective", runner=runner, now=now, local_now=LOCAL_NOW, say=lines.append)
        assert report.combined["runs_on"]["granskning"] == 1
        printed.append(lines)
    for lines in printed:
        text = "\n".join(lines)
        assert "granskning prefix: 1 tender runs it, fewer than 2, so no figures" in lines
        assert "1 fits, fewer than 2, so no planned prefix" in text and "planned prefix median" not in text
        assert "hard-stop tiers: not shown, since granskning runs on fewer than 2 tenders; 2 stopping" in text
        assert "size checks" not in text  # its counts per engine would be that one run's too
        assert "per engine only when each runs on at least 2 tenders" in text and "granskning $" not in text
    # Only the estimate line's totals, over three tenders, can differ with that one run's size.
    assert [x for x in printed[0] if not x.startswith("estimate")] == [
        x for x in printed[1] if not x.startswith("estimate")
    ]


def test_a_prefix_stop_the_check_does_not_confirm_counts_at_its_hard_stop_and_runs(data_root: Path) -> None:
    sealed_store(data_root)
    over = {"documents": 1, "unknown_documents": 0, "pages": 10, "pages_excluded": 0, "search_only": 0}
    over |= {"p1_fits": False, "planned_tokens": 900_000, "budget_tokens": 450_000}
    runner = FakeEngines({"granskning": 0.0, "kalkyl": 0.0, "baseline": 0.0}, selections={"t-0003": over})
    register(data_root)
    report = exam.run_exam(
        "co-002",
        "prospective",
        paid=True,
        budget_usd=200.0,
        runner=runner,
        now=NOW,
        local_now=LOCAL_NOW,
        say=lambda _: None,
    )
    # Confirmed at $0: nothing is estimated for it, and its Granskning run isn't started.
    assert report.runs_prefix_stop == 1 and report.estimate_usd["granskning"] == pytest.approx(2 * 0.975)
    assert runner.calls.count(("granskning", "granskning")) == 2  # of three tenders

    unconfirmed = FakeEngines(
        {"granskning": 0.0, "kalkyl": 0.0, "baseline": 0.0}, selections={"t-0003": over}, prefix_stops=False
    )
    lines: list[str] = []
    again = exam.run_exam(
        "co-002",
        "prospective",
        runner=unconfirmed,
        now=datetime(2026, 12, 1, 11, 0, tzinfo=UTC),
        local_now=LOCAL_NOW,
        say=lines.append,
    )
    # Not confirmed: counted at its hard stop, as before, and a paid exam would start it.
    assert again.estimate_usd["granskning"] == pytest.approx(2 * 0.975 + 20.0)
    assert "0 of them checked at $0 to stop before any model call" in "\n".join(lines)


def test_selection_sizes_come_from_the_engines_own_selection_and_prefix_plan(data_root: Path, tmp_path: Path) -> None:
    from large_tender import TOTAL_PAGES, write_large_prep
    from mixed_trades import write_mixed_prep

    vroot = data_root / "variant"
    write_mixed_prep(paths.dev_tender_dir("co-002", "t-0002", vroot) / "prep")
    mixed = exam.Engines(data_root / "logs", tmp_path, "HEAD", None).selection_plan(vroot, "co-002", "t-0002")
    assert mixed is not None
    # Six documents of 83 pages: plumbing, structure and the architect's plan (the tender has electrical documents)
    # are left out, and one document nothing identifies is read as unknown.
    assert (mixed["documents"], mixed["pages"], mixed["pages_excluded"], mixed["unknown_documents"]) == (6, 83, 77, 1)
    assert mixed["p1_fits"] and mixed["search_only"] == 0 and 0 < mixed["planned_tokens"] < mixed["budget_tokens"]

    large_prep = write_large_prep(tmp_path / "large" / "prep")
    budgets = tomllib.loads((CONFIG_DIR / "budgets.toml").read_text())
    large = exam.selection_sizes(large_prep, tmp_path / "large" / "calc", budgets)
    assert large is not None and large["pages"] == TOTAL_PAGES and large["pages_excluded"] > 0
    assert large["p1_fits"] and large["search_only"] > 0 and large["planned_tokens"] <= large["budget_tokens"]
    tight = exam.selection_sizes(large_prep, tmp_path / "large" / "calc", {"prefix": {"budget_tokens": 10_000}})
    assert tight is not None and tight["p1_fits"] is False

    assert exam.selection_sizes(tmp_path / "no-prep", tmp_path / "calc", budgets) is None


def test_tenders_per_hard_stop_tier_are_one_total_and_set_the_worst_case(data_root: Path) -> None:
    sealed_store(data_root)
    # Prefixes of 100,000 (tier S, $20), 250,000 (M, $30) and 1,200,000 tokens (above L: L's $45); Kalkyl's dry run
    # $1.00 high, times 1.5, is tier S ($3) for each.
    tokens = {"t-0002": 100_000, "t-0003": 250_000, "t-0004": 1_200_000}
    runner = FakeEngines({"granskning": 0.0, "kalkyl": 0.0, "baseline": 0.0}, tokens=tokens)
    lines: list[str] = []
    report = exam.run_exam("co-002", "prospective", runner=runner, now=NOW, local_now=LOCAL_NOW, say=lines.append)
    assert (
        "hard-stop tiers, tenders per tier from the dry runs, over the tenders each engine runs on: granskning S 1, "
        "M 1, L 1 (1 above the top tier); kalkyl S 3, M 0, L 0"
    ) in lines
    # Worst case: Granskning 20 + 30 + 45, Kalkyl 3 x 3, the old engine 5 inputs x 10.
    assert report.combined["worst_usd"] == 154.0


def _outline_over_the_budget() -> list:
    """An invented tender whose administrative terms' outline alone is over the 450,000-token prefix budget: even
    with every document search-only, the prefix can't fit."""
    title = "AFC.{p}{i} Villkor, ansvar och tider i avsnitt {p}, punkt {i}. " + "Rubriktext utan innehåll. " * 4
    items = [("heading", title.format(p=p, i=i), p) for p in range(1, 301) for i in range(30)]
    return [("f00000000001", "Förfrågningsunderlag/AF.pdf", 300, items)]


def test_the_prefix_stop_check_runs_granskning_at_zero_cost(data_root: Path, tmp_path: Path) -> None:
    """`anbud run --replay` in a child process, as the exam runs it, with no model key: a tender over the prefix
    budget stops on it; a tender that fits goes on to the first model call, which replay refuses."""
    from mixed_trades import write_mixed_prep

    vroot = data_root / "variant"
    for t, docs in (("t-0002", _outline_over_the_budget()), ("t-0003", None)):
        write_mixed_prep(paths.dev_tender_dir("co-002", t, vroot) / "prep", docs)
        exam._ensure_dev(vroot, "co-002", t)
    runner = exam.Engines(data_root / "logs", tmp_path, "HEAD", None)
    assert runner.granskning_stop_check(vroot, "co-002", "t-0002", "check-over") is True
    assert runner.granskning_stop_check(vroot, "co-002", "t-0003", "check-fits") is False


def test_the_prefix_stop_is_the_one_anbud_run_writes() -> None:
    from anbud.granskning.budget import PrefixPlan, over_budget

    message = over_budget(PrefixPlan(budget=3, p0=1, p1=2, p2=0, p3=0, boundary=0))
    assert f"anbud run: stopped: {message}".startswith(exam.PREFIX_STOP)
    command = (Path(__file__).resolve().parents[1] / "src" / "anbud" / "granskning" / "command.py").read_text()
    assert 'print(f"anbud run: stopped: {exc}", file=sys.stderr)' in command
    assert exam.PAID_REFUSED == cli_command.PAID_REFUSED
