"""A paid exam pass that is interrupted and run again (audit of 092b1af, items 2 and 7), and what a run reports.

Scoring plan v3.1's one-use rule: each sealed tender is attempted once per engine and input. Every paid run is put on
record (a sealed `started` line) before it starts, a pair with an attempt on record is never run again, and the
exam's budget counts what earlier passes charged. Each test names the one-line mutation of the fix that makes it
fail. Synthetic store and fake engines from test_exam; no model call, no key, a temporary data root.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_exam import LOCAL_NOW, NOW, FakeEngines, register, sealed_store

from anbud import paths
from anbud.evals import exam, freeze

COSTS = {"granskning": 8.0, "kalkyl": 1.0, "baseline": 2.0}
LATER = datetime(2026, 12, 1, 9, 30, tzinfo=UTC)


def _lines() -> list[dict]:
    return [json.loads(x) for x in freeze.ledger_file("co-002", sealed=True).read_text().splitlines()]


def _freezes() -> list[dict]:
    return [r for r in _lines() if r["kind"] == "freeze"]


def _pass(runner: FakeEngines, budget: float, now: datetime = NOW, **kw) -> exam.Report:  # type: ignore[no-untyped-def]
    return exam.run_exam(
        "co-002", "prospective", paid=True, budget_usd=budget, runner=runner, now=now, local_now=LOCAL_NOW,
        say=kw.pop("say", lambda _: None), **kw,
    )  # fmt: skip


class Interrupted(FakeEngines):
    """Runs like FakeEngines, then raises (Ctrl-C, a crash, the machine sleeping) on the first run of tender N+1."""

    def __init__(self, after_tenders: int = 1, error: type[BaseException] = KeyboardInterrupt, **kw) -> None:  # type: ignore[no-untyped-def]
        super().__init__(COSTS, **kw)
        self.after_tenders, self.error = after_tenders, error
        self.tenders: list[str] = []
        self.spent = 0.0

    def _run(self, engine, variant, vroot, company_id, tender_id, run_id):  # type: ignore[no-untyped-def]
        if tender_id not in self.tenders:
            self.tenders.append(tender_id)
        if len(self.tenders) > self.after_tenders:
            raise self.error("the pass died")
        self.spent += COSTS[engine]
        return super()._run(engine, variant, vroot, company_id, tender_id, run_id)


class Recording(FakeEngines):
    def __init__(self, **kw) -> None:  # type: ignore[no-untyped-def]
        super().__init__(COSTS, **kw)
        self.by_tender: list[tuple[str, str]] = []

    def _run(self, engine, variant, vroot, company_id, tender_id, run_id):  # type: ignore[no-untyped-def]
        self.by_tender.append((tender_id, engine))
        return super()._run(engine, variant, vroot, company_id, tender_id, run_id)


def test_a_rerun_after_an_interrupted_pass_repeats_no_attempt_and_stays_within_the_one_budget(data_root: Path) -> None:
    """Audit exam #1. Mutation: `if False and (...) in attempts:` at the paid loop's skip makes it fail."""
    sealed_store(data_root)
    register(data_root)
    first = Interrupted()
    with pytest.raises(KeyboardInterrupt):
        _pass(first, 100.0)
    done, in_flight = first.tenders
    assert {r["tender"] for r in _freezes()} == {f"co-002/{done}"}  # paid for and frozen before the interruption

    second = Recording()  # the operator runs the same command again, a little later: a new stamp, new run ids
    report = _pass(second, 100.0, now=LATER)
    assert not [(t, e) for t, e in second.by_tender if t == done]
    assert (in_flight, "granskning") not in second.by_tender  # the run in flight is never started again
    assert report.runs_attempted_before == len(first.calls) + 1
    # The two passes together stay within the one budget, counted from the sealed ledger.
    assert report.spent_before_usd + report.spent_usd <= 100.0
    assert first.spent + report.spent_usd <= 100.0
    # One attempt per tender, engine and input in the ledger.
    pairs = Counter(
        (r["tender"], r["exam"]["engine"], r["exam"]["variant"]) for r in _lines() if r["kind"] == "started"
    )
    assert set(pairs.values()) == {1}


def test_a_halted_attempt_is_never_run_again_even_when_the_rerun_would_finish(data_root: Path) -> None:
    """Audit replay #2. Mutation: `if False and (...) in attempts:` at the paid loop's skip makes it fail."""
    sealed_store(data_root)
    register(data_root)
    first = Interrupted(ends={"granskning": freeze.HALTED})
    with pytest.raises(KeyboardInterrupt):
        _pass(first, 100.0)
    key = f"co-002/{first.tenders[0]}"
    before = Counter((r["tender"], r["exam"]["engine"], r["status"]) for r in _freezes())
    assert before[(key, "granskning", "halted")] == 1

    _pass(FakeEngines(COSTS), 100.0, now=LATER)  # everything would finish now
    after = Counter((r["tender"], r["exam"]["engine"], r["status"]) for r in _freezes())
    assert after[(key, "granskning", "finished")] == 0, "a halted (no prediction) tender got a second, finished run"
    assert sum(n for (t, _, _), n in after.items() if t == key) == sum(n for (t, _, _), n in before.items() if t == key)
    cutoff = datetime(2027, 1, 1, tzinfo=UTC)
    assert freeze.scored_prediction(_lines(), key, "granskning", "granskning", cutoff) is None


class Crash(RuntimeError):
    """Stands in for an error the paid loop doesn't catch (Engines.baseline's export, a run.json read)."""


def test_a_run_in_flight_when_the_pass_died_is_no_prediction_and_never_rerun(data_root: Path) -> None:
    """Audit halts #3. Mutation: replacing the `freeze.start_run(...)` call with `pass` makes it fail."""
    sealed_store(data_root)
    register(data_root)
    first = Interrupted(error=Crash)
    with pytest.raises(Crash):
        _pass(first, 500.0, engines=("granskning",))
    done, in_flight = first.tenders
    second = Recording()
    _pass(second, 500.0, now=LATER, engines=("granskning",))
    ran = {t for t, _ in second.by_tender}
    assert done not in ran and in_flight not in ran
    assert freeze.freezes_by_tender(freeze.ledger_file("co-002", sealed=True))[f"co-002/{done}"] == 1
    # The attempt in flight is on record, started and never frozen: no prediction.
    summary = exam.paid_summary("co-002")
    assert summary["ended"][exam.NEVER_FROZEN] == {"granskning": 1} and summary["attempts"] == summary["pairs"]
    cutoff = datetime(2027, 1, 1, tzinfo=UTC)
    assert freeze.scored_prediction(_lines(), f"co-002/{in_flight}", "granskning", "granskning", cutoff) is None


def test_the_budget_counts_what_earlier_passes_charged_and_a_lost_run_at_its_cap(data_root: Path) -> None:
    """Audit exam #1, the spend. Mutation: `report.spent_before_usd = 0.0` in the paid stage makes it fail."""
    sealed_store(data_root)
    register(data_root)
    first = Interrupted()
    with pytest.raises(KeyboardInterrupt):
        _pass(first, 100.0)
    caps = {r["run_id"]: r["exam"]["cap_usd"] for r in _lines() if r["kind"] == "started"}
    frozen = {r["run_id"]: r["exam"]["charged_usd"] for r in _freezes()}
    lost = sum(cap for run, cap in caps.items() if run not in frozen)
    assert lost > 0 and sum(frozen.values()) == pytest.approx(first.spent)

    report = _pass(FakeEngines(COSTS), 100.0, now=LATER)
    assert report.spent_before_usd == pytest.approx(first.spent + lost)  # the run that never came back: its cap
    assert report.combined["paid"]["charged_usd"] == pytest.approx(report.spent_before_usd + report.spent_usd)
    # A third pass at a budget the earlier passes already used is refused before anything starts, with no figure of
    # the split's own.
    with pytest.raises(exam.ExamError, match="above the budget") as info:
        _pass(FakeEngines(COSTS), report.spent_before_usd + report.spent_usd - 0.01, now=LATER.replace(minute=45))
    assert not re.search(r"\$\d", str(info.value).split("above the budget")[0])


ONE_LEFT = {"t-0003": "manifest", "t-0004": "manifest"}  # of three prospective tenders only t-0002 is prepared


def test_the_budget_refusal_names_no_figure_for_the_one_prepared_tender(data_root: Path) -> None:
    """Audit exam #3. Mutation: adding `({report.estimate_total:.2f})` to `_refusal`'s message makes it fail."""
    sealed_store(data_root)
    register(data_root)
    with pytest.raises(exam.ExamError) as info:
        _pass(FakeEngines(COSTS, stops=ONE_LEFT), 0.5)
    message = str(info.value)
    assert "above the budget ($0.50)" in message
    assert not re.search(r"\$\d", message.split("above the budget")[0]), message
    assert "both splits" not in message  # the total covers one tender too, so it isn't shown


def test_a_paid_pass_with_one_prepared_tender_prints_no_figure_of_its_runs(data_root: Path) -> None:
    """Audit exam #3. Mutation: printing the pass's `spent_usd` in `_say` makes it fail."""
    sealed_store(data_root)
    register(data_root)
    lines: list[str] = []
    report = _pass(FakeEngines(COSTS, stops=ONE_LEFT), 1000.0, say=lines.append)
    assert report.prepared == 1 and report.tenders == 3
    assert report.spent_usd > 0  # one tender's runs did spend
    assert not [line for line in lines if re.search(r"\$\d", line)], lines  # no figure in dollars at all
    assert report.owed == 2  # the two tenders whose prep stopped for a reason a later pass retries


DEGRADED = {
    "refused_calls": 3,
    "lenses_not_run": ["l7"],
    "lenses_stopped": ["l6"],
    "partial": True,
    "verifier": {"selected": 12, "verified": 9, "not_verified_budget": 3},
}
FULL = {**DEGRADED, "refused_calls": 0, "lenses_not_run": [], "lenses_stopped": [], "partial": False,
        "verifier": {"selected": 12, "verified": 12, "not_verified_budget": 0}}  # fmt: skip


def _both(runner: FakeEngines, budget: float = 500.0, now: datetime = NOW, **kw) -> tuple[list[exam.Report], list[str]]:  # type: ignore[no-untyped-def]
    lines: list[str] = []
    reports = exam.run_both(
        "co-002", paid=True, budget_usd=budget, runner=runner, now=now, local_now=LOCAL_NOW, say=lines.append, **kw
    )
    return reports, lines


def test_a_degraded_run_is_frozen_finished_with_its_summary_and_counted_in_the_exam_s_one_total(
    data_root: Path,
) -> None:
    """Mutation: dropping the `degraded` field from the freeze line's `exam` makes it fail."""
    sealed_store(data_root)
    register(data_root)
    budgets = {"t-0001": FULL, "t-0002": DEGRADED, "t-0003": FULL, "t-0004": FULL}
    reports, lines = _both(FakeEngines(COSTS, budgets=budgets))
    reviews = {r["tender"]: r for r in _freezes() if r["exam"]["engine"] == "granskning"}
    assert reviews["co-002/t-0002"]["status"] == "finished"
    less = reviews["co-002/t-0002"]["exam"]["degraded"]  # partial and the verifier counts on the line, in counts
    assert less["any"] and less["partial"] and less["lenses_not_run"] == less["lenses_stopped"] == 1
    assert (less["verifier_selected"], less["verifier_verified"], less["not_verified_budget"]) == (12, 9, 3)
    assert reviews["co-002/t-0003"]["exam"]["degraded"]["any"] is False
    assert [r.runs_degraded for r in reports] == [{"granskning": 1}, {}]
    assert "; 1 finished with less than a full review;" in lines[-1]  # one total over both splits only


@pytest.mark.parametrize(
    ("code", "stdout", "charged"),
    [
        (0, "  flags: 4 after merge\ntotal $2.00 (budget spent $1.95 of $20.00)\nreport: r\n", 1.95),
        (0, "total $2.00\nreport: r\nbudget spent $1.95 of $20.00\n", 1.95),  # its own last line, from a finally
        (1, "  map: ok, $0.40\nbudget spent $3.10 of $20.00\n", 3.10),  # a crash that still reports its spend
        (1, "  map: ok, $0.40\n", None),  # no line: the exam charges the run's tier cap
    ],
)
def test_granskning_s_reported_spend_is_charged_whatever_its_exit(
    data_root: Path, code: int, stdout: str, charged: float | None
) -> None:
    """Mutation: `float(spent[-1]) if spent and code == 0 else None` makes the crash case fail."""
    vroot = data_root / "vroot"
    record = paths.company_dir("co-002", vroot) / "runs" / "r1"
    record.mkdir(parents=True)
    (record / "granskning.json").write_text(json.dumps({"budget": DEGRADED, "degraded": LOST}))
    engines = exam.Engines(data_root / "logs", data_root / "app", "main", data_root)
    done = subprocess.CompletedProcess(["anbud"], code, stdout, "")
    engines._anbud = lambda *_: done  # type: ignore[method-assign]
    one = engines.granskning(vroot, "co-002", "t-0002", "r1")
    assert one.cost_usd == charged and one.ok is (code == 0)
    assert one.budget == DEGRADED and one.lost == LOST and one.degraded


LOST = {"lenses_lost": ["l3"], "map_lost": False, "retries": 2, "failed_calls": 1, "failures": {"server": 1}}


@pytest.mark.parametrize(
    ("budget", "lost", "degraded"),
    [
        (FULL, {**LOST, "lenses_lost": [], "failed_calls": 0, "failures": {}}, False),  # retries that recovered
        (FULL, {**LOST, "failed_calls": 0, "failures": {}}, True),  # a lens lost to an error
        (FULL, {**LOST, "lenses_lost": [], "map_lost": True, "failed_calls": 0, "failures": {}}, True),
        (FULL, {**LOST, "lenses_lost": []}, True),  # a call lost for good: a page card's tile, a verdict
        (DEGRADED, None, True),
        (None, None, False),
    ],
)
def test_a_run_is_degraded_by_lost_work_never_by_a_retry_that_recovered(budget, lost, degraded) -> None:  # type: ignore[no-untyped-def]
    """Mutation: adding `or d.get("retries")` to `is_degraded` makes the first case fail."""
    assert exam.is_degraded(budget, lost) is degraded
    summary = exam.degraded_summary(budget, lost)
    assert summary is None if budget is None and lost is None else summary["any"] is degraded
