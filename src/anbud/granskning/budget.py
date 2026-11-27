"""The prefix budget: what the shared prefix holds in full when the selected documents don't all fit.

Every Opus call in a run sends the whole prefix, so its size sets the run's cost and must stay inside the model's
context. After the selection (`anbud.selection`) has left other trades' documents out, the prefix is planned against
a budget in tokens (config/budgets.toml `[prefix]`), with sizes estimated from characters the same way as the run's
own estimate, so the plan is deterministic and a rebuild sends the same bytes:

- P0, always in full: the brief, the rules and the calc.
- P1, always in full: the administrative terms (AF), the contract, the general description (rambeskrivning), the
  time plan and the electrical technical description. If P0 and P1 alone exceed the budget, the run stops before any
  paid call (`PrefixOverBudget`); the dry run shows P1 on its own line.
- P2: the electrical drawings and lists, the boundary and object lists, the other electrical documents, and the lines
  brought back from other trades' documents (capped at a share of the budget).
- P3: the other common documents and the unclassified ones.

Over the budget, documents become search-only, P3 before P2 and the largest first within each: the prefix keeps the
document's outline (headings with block ids and pages), and search_blocks and read_blocks reach its text. Boundary
lines beyond their cap leave the prefix in reading order from the last. Nothing is cut silently: the plan, the run
summary and the report name every search-only document.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from anbud.granskning.contracts import CalcPost
from anbud.granskning.corpus import Corpus
from anbud.granskning.prefix import CHARS_PER_TOKEN, block_line, outline_lines, prefix_chars, system_blocks
from anbud.selection import classify, views

P1_ROLES = ("af", "contract", "general_description", "time_plan", "el_description")
P2_ROLES = ("el_list", "el_drawing", "el", "boundary_list", "object_list")
SEARCH_ONLY = "prefixbudget"  # why a document is search-only
MARK_CHARS = 1000  # the search-only note and marks the plan adds to the document list
DOC_CHARS = 100  # a document's <doc> and </doc> lines


class PrefixOverBudget(RuntimeError):
    """P0 and P1 alone exceed the prefix budget: raised before any paid call, with counts only."""


@dataclass(frozen=True)
class PrefixBudget:
    tokens: int
    boundary_share: float

    @classmethod
    def from_config(cls, budgets: dict[str, Any]) -> PrefixBudget | None:
        raw = budgets.get("prefix")
        if not raw:
            return None
        return cls(int(raw["budget_tokens"]), float(raw.get("boundary_share", 0.10)))


@dataclass
class PrefixPlan:
    budget: int
    p0: int  # brief, rules and calc
    p1: int
    p2: int
    p3: int
    boundary: int  # lines brought back from other trades' documents, before their cap
    priority: dict[str, int] = field(default_factory=dict)  # doc_id -> 1, 2 or 3
    search_only: list[str] = field(default_factory=list)  # doc ids, in the order they were demoted
    boundary_dropped: list[str] = field(default_factory=list)  # block ids past the cap
    total: int = 0  # the planned prefix, estimated

    @property
    def p1_fits(self) -> bool:
        return self.p0 + self.p1 <= self.budget

    def summary(self) -> dict[str, Any]:
        """Counts only: safe to print and to log."""
        return {
            "budget_tokens": self.budget,
            "p0_tokens": self.p0,
            "p1_tokens": self.p1,
            "p2_tokens": self.p2,
            "p3_tokens": self.p3,
            "boundary_tokens": self.boundary,
            "planned_tokens": self.total,
            "search_only_documents": len(self.search_only),
            "boundary_lines_dropped": len(self.boundary_dropped),
            "p1_fits": self.p1_fits,
        }


def _tokens(chars: int) -> int:
    return int(chars / CHARS_PER_TOKEN)


def priorities(corpus: Corpus) -> dict[str, int]:
    """Each read document's priority, from its classification's role."""
    out = {}
    for view in views(corpus):
        if view.doc_id in corpus.excluded:
            continue
        role = classify(view).role
        out[view.doc_id] = 1 if role in P1_ROLES else 2 if role in P2_ROLES else 3
    return out


def _doc_chars(corpus: Corpus) -> dict[str, int]:
    chars: dict[str, int] = {}
    for block_id in corpus.order:
        block = corpus.blocks[block_id]
        chars[block.doc_id] = chars.get(block.doc_id, DOC_CHARS) + len(block_line(block)) + 1
    return chars


def plan_prefix(corpus: Corpus, calc: list[CalcPost], budget: PrefixBudget) -> PrefixPlan:
    """What stays in full and what becomes search-only. Deterministic: sizes from characters, ties by doc id."""
    p0 = _tokens(prefix_chars(system_blocks(replace(corpus, order=[]), calc)) + MARK_CHARS)  # all but document text
    chars = _doc_chars(corpus)
    prio = priorities(corpus)
    boundary_ids = [b for b in corpus.order if corpus.blocks[b].doc_id in corpus.excluded]
    sizes = {d: _tokens(n) for d, n in chars.items() if d not in corpus.excluded}
    by = {k: sum(t for d, t in sizes.items() if prio.get(d, 3) == k) for k in (1, 2, 3)}
    tags = DOC_CHARS * len({corpus.blocks[b].doc_id for b in boundary_ids})
    boundary = _tokens(tags + sum(len(block_line(corpus.blocks[b])) + 1 for b in boundary_ids))
    plan = PrefixPlan(budget.tokens, p0, by[1], by[2], by[3], boundary, prio)
    if not plan.p1_fits:
        plan.total = p0 + sum(by.values()) + boundary
        return plan

    cap = int(budget.tokens * budget.boundary_share)
    kept = boundary
    for block_id in reversed(boundary_ids):
        if kept <= cap:
            break
        kept -= _tokens(len(block_line(corpus.blocks[block_id])) + 1)
        plan.boundary_dropped.insert(0, block_id)

    total = p0 + sum(by.values()) + kept
    outline = _outline_tokens(corpus)
    for level in (3, 2):
        for doc_id in sorted((d for d in sizes if prio.get(d, 3) == level), key=lambda d: (-sizes[d], d)):
            if total <= budget.tokens:
                break
            total -= sizes[doc_id] - outline.get(doc_id, 0)
            plan.search_only.append(doc_id)
    plan.total = total
    return plan


def _outline_tokens(corpus: Corpus) -> dict[str, int]:
    out: dict[str, int] = {}
    for doc_id in {corpus.blocks[b].doc_id for b in corpus.order}:
        out[doc_id] = _tokens(DOC_CHARS + sum(len(line) + 1 for line in outline_lines(corpus, doc_id)))
    return out


def apply_plan(corpus: Corpus, plan: PrefixPlan) -> Corpus:
    """The corpus the prefix is built from: search-only documents and dropped boundary lines leave `order`; every
    block stays in `blocks` and `all_order`."""
    if not plan.search_only and not plan.boundary_dropped:
        return corpus
    gone = set(plan.boundary_dropped)
    demoted = set(plan.search_only)
    order = [b for b in corpus.order if b not in gone and corpus.blocks[b].doc_id not in demoted]
    search_only = {**corpus.search_only, **{d: SEARCH_ONLY for d in plan.search_only}}
    return replace(corpus, order=order, search_only=search_only)


def fit_prefix(corpus: Corpus, calc: list[CalcPost], budgets: dict[str, Any]) -> tuple[Corpus, PrefixPlan | None]:
    """The planned corpus, or the corpus as it is when config sets no budget. Raises when P1 doesn't fit."""
    budget = PrefixBudget.from_config(budgets)
    if budget is None:
        return corpus, None
    plan = plan_prefix(corpus, calc, budget)
    if not plan.p1_fits:
        raise PrefixOverBudget(over_budget(plan))
    return apply_plan(corpus, plan), plan


def over_budget(plan: PrefixPlan) -> str:
    return (
        f"the prefix's fixed part ({plan.p0} tokens) and its priority-1 documents ({plan.p1} tokens) exceed the "
        f"budget of {plan.budget} tokens"
    )
