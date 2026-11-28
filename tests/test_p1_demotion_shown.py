"""When priority 1 fits on its own (`p1_fits`) but not with the other documents' outlines and the other trades' lines,
the plan still demotes priority-1 documents. The paid run's progress line, the HTML, the xlsx and the lens notes name
them; the run record (`prefix_plan` in granskning.json) and the dry run must too. Synthetic large tender, $0.

Each test names the one-line mutation it catches; each was made, seen to fail the test, and reverted."""

from __future__ import annotations

import pytest
from large_tender import write_large_prep

from anbud.config import load_config
from anbud.granskning.budget import PrefixBudget, apply_plan, p1_search_only, plan_prefix
from anbud.granskning.command import dry_run
from anbud.granskning.corpus import Corpus
from anbud.granskning.rank import coverage
from anbud.selection import load_selected


@pytest.fixture(scope="module")
def selected(tmp_path_factory: pytest.TempPathFactory) -> Corpus:
    prep = write_large_prep(tmp_path_factory.mktemp("large") / "t" / "prep")
    corpus, _ = load_selected(prep)
    return corpus


def _p1_exactly(corpus: Corpus) -> PrefixBudget:
    """P0 and P1 fill the budget exactly: P1 fits, but the outlines of P2 and P3 and the kept boundary lines don't."""
    wide = plan_prefix(corpus, [], PrefixBudget(10_000_000, 0.10))
    return PrefixBudget(wide.p0 + wide.p1, 0.10)


def test_the_run_record_names_demoted_priority_1_documents_even_when_priority_1_fits(selected: Corpus) -> None:
    """Mutation: `if not self.p1_fits or self.p1_search_only or self.core_search_only` -> `if not self.p1_fits` in
    `PrefixPlan.summary`: the record drops the demoted priority-1 documents the report and the lenses name."""
    plan = plan_prefix(selected, [], _p1_exactly(selected))
    assert not plan.stops and plan.p1_fits and plan.p1_search_only  # the trigger
    applied = apply_plan(selected, plan)
    assert p1_search_only(applied) and coverage([], applied).search_only_priority_1  # the lens note, HTML, xlsx
    record = plan.summary()
    assert record["p1_search_only"] == list(plan.p1_search_only)
    assert record["p1_search_only_documents"] == len(plan.p1_search_only)
    assert record["core_search_only"] == list(plan.core_search_only)
    assert record["p1_fits"] is True  # the property itself is recorded as it is


def test_the_dry_run_reports_the_priority_1_demotion_the_paid_run_makes(
    selected: Corpus, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutation: `if plan.p1_search_only` -> `if False` in `dry_run`'s "priority 1 fits" branch: the dry run, made
    before the exam to check sizes, says nothing of the priority-1 documents the paid run demotes."""
    budget = _p1_exactly(selected)
    plan = plan_prefix(selected, [], budget, selected.tiles.values())
    assert plan.p1_fits and plan.p1_search_only  # the trigger
    budgets = dict(load_config().budgets)
    budgets["prefix"] = {"budget_tokens": budget.tokens, "boundary_share": 0.10}
    dry_run(selected, [], [], None, budgets, {"dirty": False, "diff_sha256": None, "harness_sha": "abc"}, None)
    out = capsys.readouterr().out
    n, core = len(plan.p1_search_only), len(plan.core_search_only)
    assert "priority 1 fits (" in out
    assert f"yet {n} priority-1 documents become search-only, {core} of them the core's" in out
