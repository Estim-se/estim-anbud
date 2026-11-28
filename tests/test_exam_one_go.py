"""The paid exam in one invocation (`anbud eval exam`): prospective, then held-out, and one total over both splits,
printed only by the invocation that completes the exam. The held-out split runs last, so a figure printed before the
exam is complete, beside that total, would give the held-out runs by subtraction. Each test names the one-line
mutation of the fix that makes it fail. Synthetic store and fake engines from test_exam; no model call.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from pathlib import Path

import pytest
from test_exam import LOCAL_NOW, NOW, FakeEngines, register, sealed_store

from anbud import cli
from anbud.evals import exam, freeze

COSTS = {"granskning": 8.0, "kalkyl": 1.0, "baseline": 2.0}
HELD = "t-0001"  # sealed_store's held-out tender


class HeldOutEnds(FakeEngines):
    """The held-out tender's runs end and cost as told; every other tender's as FakeEngines does."""

    def __init__(self, held: dict[str, tuple[str, float]]) -> None:
        super().__init__(COSTS)
        self.held = held

    def _run(self, engine, variant, vroot, company_id, tender_id, run_id):  # type: ignore[no-untyped-def]
        one = super()._run(engine, variant, vroot, company_id, tender_id, run_id)
        if tender_id != HELD:
            return one
        end, cost = self.held[engine]
        ok = end == freeze.FINISHED
        return dataclasses.replace(one, ok=ok, exit_code=0 if ok else 1, halted=end == freeze.HALTED, cost_usd=cost)


def _both(runner: FakeEngines, budget: float | None, at: int) -> list[str]:
    lines: list[str] = []
    exam.run_both(
        "co-002",
        paid=budget is not None,
        budget_usd=budget,
        runner=runner,
        now=NOW + timedelta(minutes=at),
        local_now=LOCAL_NOW,
        say=lines.append,
    )
    return lines


def _exam(root: Path, monkeypatch: pytest.MonkeyPatch, held: dict[str, tuple[str, float]]) -> list[list[str]]:
    """One exam in its own data root: a paid invocation the budget leaves incomplete (the held-out review isn't
    started), a $0 pass, and the paid invocation that completes it. What each printed."""
    root.mkdir()
    monkeypatch.setenv("ANBUD_DATA_ROOT", str(root))
    sealed_store(root)
    register(root)
    first = _both(HeldOutEnds(held), 50.0, 0)
    zero = _both(FakeEngines(COSTS), None, 1)
    last = _both(HeldOutEnds(held), 500.0, 2)
    return [first, zero, last]


def test_no_line_printed_before_the_exam_is_complete_depends_on_the_held_out_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two exams whose held-out runs end and cost differently print the same lines until the one total, the last line
    of the invocation that completes the exam. Mutation: printing the total whatever the exam owes (`if True:` for
    run_both's completeness check) makes it fail."""
    a = _exam(tmp_path / "a", monkeypatch, {e: (freeze.FINISHED, c) for e, c in COSTS.items()})
    b = _exam(
        tmp_path / "b",
        monkeypatch,
        {"granskning": (freeze.HALTED, 19.5), "kalkyl": (freeze.FAILED, 2.9), "baseline": (freeze.FINISHED, 9.0)},
    )
    first, zero, last = a
    assert first == b[0] and zero == b[1] and last[:-1] == b[2][:-1]
    assert first[-1].startswith("paid exam: not complete")  # the budget kept the held-out review from starting
    assert last[-1].startswith("paid exam: complete. One total over both splits") and last[-1] != b[2][-1]
    # Before that total, no line gives a figure of the paid runs: none is a runs line, none says what was charged.
    earlier = first + zero + last[:-1]
    assert not [x for x in earlier if x.startswith(("runs", "paid exam: complete")) or "charged" in x or "spent" in x]


def test_one_invocation_runs_both_splits_in_order_and_its_total_matches_the_sealed_ledger(data_root: Path) -> None:
    sealed_store(data_root)
    register(data_root)
    reports = exam.run_both("co-002", paid=True, budget_usd=500.0, runner=FakeEngines(COSTS), now=NOW,
                            local_now=LOCAL_NOW, say=(lines := []).append)  # fmt: skip
    assert [r.kind for r in reports] == list(exam.ORDER) == ["prospective", "heldout"]
    assert not any(r.owed for r in reports)
    total = exam.paid_summary("co-002")
    assert total["tenders"] == 4 and total["attempts"] == total["pairs"] == sum(r.frozen for r in reports)
    assert total["charged_usd"] == pytest.approx(sum(r.spent_usd for r in reports))
    assert [x for x in lines if "charged" in x] == [lines[-1]]  # printed once, last
    assert f"charged ${total['charged_usd']:.2f}" in lines[-1]
    # Held-out runs come after their deadline by design, so only prospective ones count as late.
    assert total["late"] == reports[0].runs_after_deadline > 0
    assert f"; {total['late']} prospective runs finished after their tender's deadline;" in lines[-1]


def test_an_invocation_that_leaves_runs_for_the_budget_prints_no_figure_of_its_runs(data_root: Path) -> None:
    """Mutation: `if True:` for run_both's completeness check makes it fail."""
    sealed_store(data_root)
    register(data_root)
    lines = _both(FakeEngines(COSTS), 50.0, 0)
    assert lines[-1] == (
        "paid exam: not complete; its one total over both splits prints when every tender has an attempt for every "
        "engine and input"
    )
    assert not [x for x in lines if "charged" in x or x.startswith("runs")]


def test_the_budget_check_counts_the_other_split_s_runs_left_before_anything_starts(data_root: Path) -> None:
    """The held-out split runs last: a budget that fits the prospective runs alone is refused before the first starts,
    so the exam isn't left half run. Mutation: `return 0.0` at the top of `_owed_elsewhere` makes it fail."""
    sealed_store(data_root)
    register(data_root)
    zero = exam.run_both("co-002", runner=FakeEngines(COSTS), now=NOW, local_now=LOCAL_NOW, say=lambda _: None)
    prospective, total = zero[0].estimate_total, round(sum(zero[-1].combined["estimate_usd"].values()), 2)
    assert prospective < total
    with pytest.raises(exam.ExamError, match="above the budget"):
        _both(FakeEngines(COSTS), (prospective + total) / 2, 1)
    assert not freeze.ledger_file("co-002", sealed=True).exists()  # nothing started, nothing on record


def test_a_paid_split_alone_is_refused_and_the_exam_s_exit_says_nothing_of_its_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutation: `if False:` for the CLI's refusal of a paid split alone makes it fail."""
    monkeypatch.setattr(cli, "held_out_refusal", lambda: None)
    for kind in ("prospective", "heldout"):
        assert cli.main(["eval", kind, "co-002", "--paid", "--budget", "10"]) == cli.REFUSED
        assert "a paid exam runs both splits in one invocation" in capsys.readouterr().err
    kept = exam.Report(kind="prospective", prep_stops={"security_marking": 1}, runs_failed={"kalkyl": 2})
    retried = exam.Report(kind="heldout", prep_stops={"read_error": 1})
    monkeypatch.setattr(exam, "run_both", lambda *a, **kw: [kept, exam.Report(kind="heldout")])
    assert cli.main(["eval", "exam", "co-002"]) == 0  # a kept stop and failed runs: neither is in the exit code
    monkeypatch.setattr(exam, "run_both", lambda *a, **kw: [kept, retried])
    assert cli.main(["eval", "exam", "co-002"]) == 1  # a prep stop a later pass retries
