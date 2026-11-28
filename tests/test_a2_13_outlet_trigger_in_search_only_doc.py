"""The outlet recipe check finds its trigger in a search-only document too (audit A2-13). The prefix plan takes a
document that doesn't fit out of `order` and keeps only its outline, but its text stays searchable. The trigger walk
went over `order` alone, so a trigger in that document was missed and the check skipped. It now walks every document
but another trade's. Invented text and codes, $0.

Each test names the one-line mutation it catches; each was made, seen to fail the test, and reverted."""

from __future__ import annotations

from dataclasses import replace

from test_granskning_checks import DOC_FU, corpus

from anbud.granskning.budget import PrefixPlan, apply_plan
from anbud.granskning.check_dry import checks_line
from anbud.granskning.outlet_recipe import trigger_hints

TRIGGER = "f0f0f0-000002"  # names the standard, with outlet words beside it (`test_granskning_checks.BLOCKS`)


def test_a_trigger_in_a_search_only_document_is_found() -> None:
    """Mutation: `for block_id in corpus.all_order:` -> `for block_id in corpus.order:` in `trigger_hints`: the
    search-only document isn't walked, and the check is skipped as if the tender had no trigger."""
    planned = apply_plan(corpus(), PrefixPlan(0, 0, 0, 0, 0, 0, search_only=[DOC_FU]))
    assert DOC_FU in planned.search_only and TRIGGER not in planned.order and TRIGGER in planned.all_order
    assert trigger_hints(planned) == [TRIGGER]
    assert "outlet trigger candidates 1 (1 call)" in checks_line(planned, [], None)  # the dry run counts it too


def test_a_trigger_in_another_trade_s_document_is_still_left_out() -> None:
    """The selection leaves another trade's documents out of `order` on purpose. Mutation: the
    `if corpus.blocks[block_id].doc_id in corpus.excluded:` guard's `continue` -> `pass` in `trigger_hints`: another
    trade's trigger starts the check."""
    full = corpus()
    other = replace(full, order=[b for b in full.order if full.blocks[b].doc_id != DOC_FU], excluded={DOC_FU: "VS"})
    assert TRIGGER in other.all_order
    assert trigger_hints(other) == []
