"""The prefix plan errs high, never low (test_prefix_budget's contract). It counts each document's <doc> lines by their
length, id and file name included, and each search-only document's mark in both places it goes: the document list and
its <doc> line. With a flat count for both, long file names and many demoted documents rendered the prefix above the
plan and the budget. Synthetic corpora only, $0.

Each test names the one-line mutation it catches; each was made, seen to fail the test, and reverted."""

from __future__ import annotations

from collections.abc import Callable

from anbud.granskning.budget import PrefixBudget, apply_plan, plan_prefix
from anbud.granskning.corpus import Corpus
from anbud.granskning.prefix import estimate_tokens, system_blocks
from anbud.models import Block


def _corpus(n_docs: int, name_for: Callable[[int], str], words: int = 200) -> Corpus:
    blocks: list[Block] = []
    docs = []
    for d in range(n_docs):
        doc = f"doc{d:03d}xxxxxx"  # 12-character ids, as prep makes them
        docs.append({"doc_id": doc, "paths": [f"inbox/{name_for(d)}"]})
        blocks.append(Block(id=f"{doc}-h", doc_id=doc, kind="heading", text="Rubrik", page=1))
        blocks.append(Block(id=f"{doc}-p", doc_id=doc, kind="paragraph", text="text " * words, page=1))
    return Corpus({b.id: b for b in blocks}, [b.id for b in blocks], [], {}, docs)


def _rendered(corpus: Corpus, budget: PrefixBudget) -> tuple[int, int]:
    plan = plan_prefix(corpus, [], budget)
    assert not plan.stops and plan.total <= budget.tokens
    return estimate_tokens(system_blocks(apply_plan(corpus, plan), [])), plan.total


def test_long_file_names_near_the_floor_render_within_the_plan() -> None:
    """Mutation: `TAG_CHARS + len(d) + len(names.get(d, ""))` -> `TAG_CHARS + len(d)` in `_tag_chars`: the names go
    uncounted, and 200 long ones render the prefix above the plan."""
    name = "E-63-1-{:03d} Teknisk beskrivning el tillagg nummer tre revision B daterad 2026-10-01.pdf"
    corpus = _corpus(200, name.format)
    floor = plan_prefix(corpus, [], PrefixBudget(10**9, 0.1)).floor
    rendered, planned = _rendered(corpus, PrefixBudget(floor + 10, 0.1))
    assert rendered <= planned


def test_many_demoted_documents_render_within_the_budget() -> None:
    """Mutation: `return 2 * (len(' search_only=""') + len(SEARCH_ONLY_CORE))` -> `return 0` in `_mark_chars`: 300
    documents' marks go uncounted, and the prefix renders above the budget."""
    corpus = _corpus(300, lambda d: f"Bilaga {d:03d} Underlag till forfragan rev B.pdf")  # ordinary names, priority 3
    full = plan_prefix(corpus, [], PrefixBudget(10**9, 0.1))
    assert set(full.priority.values()) == {3}
    budget = PrefixBudget(full.floor, 0.1)
    assert len(plan_prefix(corpus, [], budget).search_only) == 300  # every one demoted
    rendered, planned = _rendered(corpus, budget)
    assert rendered <= planned <= budget.tokens
