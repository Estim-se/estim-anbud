"""The prefix budget: what the shared prefix holds in full when the selected documents don't all fit.

Every Opus call in a run sends the whole prefix, so its size sets the run's cost and must stay inside the model's
context. After the selection (`anbud.selection`) has left other trades' documents out, the prefix is planned against
a budget in tokens (config/budgets.toml `[prefix]`), with sizes estimated from characters the same way as the run's
own estimate, so the plan is deterministic and a rebuild sends the same bytes:

- P0, always in full: the brief, the rules and the calc.
- P1: the administrative terms (AF), the contract, the general description (rambeskrivning), the time plan and the
  electrical technical description. The AF and the contract are the core: always in full.
- P2: the electrical drawings and lists, the boundary and object lists, the other electrical documents, and the lines
  brought back from other trades' documents (capped at a share of the budget).
- P3: the other common documents and the unclassified ones.

Over the budget, documents become search-only, P3 before P2 before P1, the largest first within each: the prefix keeps
the document's outline (headings with block ids and pages), and search_blocks and read_blocks reach its text. Of P1,
only what isn't core is demoted, and a small time plan goes last. A large tender gets a partial review, with its P1
documents reached by search, instead of none. Boundary lines beyond their cap leave the prefix in reading order from
the last. Nothing is cut silently: the plan, the run summary and the report name every search-only document, and the
report and the lenses are told which P1 documents were read through search (`SEARCH_ONLY_P1`).

The run stops before any paid call (`PrefixOverBudget`) only when P0 and the core alone exceed the budget. The page
cards are read before the plan is made, and their text joins its documents, so the check before the cards counts for
each tile the most text its card can add (`card_tokens`): a run that the core's cards would take over the budget stops
before the first card is paid for, not after. The dry run shows P1 and the core on their own lines.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from anbud.granskning.contracts import CalcPost
from anbud.granskning.corpus import Corpus
from anbud.granskning.prefix import CHARS_PER_TOKEN, block_line, outline_lines, prefix_chars, system_blocks
from anbud.models import Block
from anbud.prep.model import Tile
from anbud.selection import HEADER_BLOCKS, HEADER_CHARS, classify, views

P1_ROLES = ("af", "contract", "general_description", "time_plan", "el_description")
CORE_ROLES = ("af", "contract")  # of P1, always in full
SMALL_TIME_PLAN = 0.05  # a time plan up to this share of the budget is demoted last of P1
P2_ROLES = ("el_list", "el_drawing", "el", "boundary_list", "object_list")
SEARCH_ONLY = "prefixbudget"  # why a document is search-only
SEARCH_ONLY_P1 = "prefixbudget, priority 1"  # a P1 document demoted: its text is reached by search
MARK_CHARS = 1000  # the search-only note and marks the plan adds to the document list
DOC_CHARS = 100  # a document's <doc> and </doc> lines
# At most one tile's card text, and its paragraphs (each a block line). About three dense A4 pages of text: the cards
# measured so far are far smaller (the dev tender's largest is under 2000 characters).
CARD_CHARS = 12_000
CARD_BLOCKS = 60


class PrefixOverBudget(RuntimeError):
    """P0 and the core of P1 (AF and contract) alone exceed the prefix budget: raised before any paid call, with counts
    only."""


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
    cards: int = 0  # at most what the page cards still to be read add to P1 (`card_tokens`); 0 once they are in
    core: int = 0  # the AF and the contract: P1 that is never demoted
    core_cards: int = 0  # at most what the page cards still to be read add to the core
    p1_search_only: list[str] = field(default_factory=list)  # the P1 doc ids of `search_only`, in the same order

    @property
    def p1_fits(self) -> bool:
        """All of P1 fits in full."""
        return self.p0 + self.p1 + self.cards <= self.budget

    @property
    def stops(self) -> bool:
        """P0 and the core don't fit: the run stops before any paid call."""
        return self.p0 + self.core + self.core_cards > self.budget

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
            **({"p1_card_tokens_max": self.cards} if self.cards else {}),  # a plan after the cards stays as it was
            # only when P1 overflows: a plan where it fits stays as it was
            **(
                {
                    "core_tokens": self.core,
                    "core_card_tokens_max": self.core_cards,
                    "stops": self.stops,
                    "p1_search_only_documents": len(self.p1_search_only),
                    "p1_search_only": list(self.p1_search_only),
                }
                if not self.p1_fits
                else {}
            ),
        }


def _tokens(chars: int) -> int:
    return int(chars / CHARS_PER_TOKEN)


def roles(corpus: Corpus) -> dict[str, str | None]:
    """Each read document's role, from its classification."""
    return {v.doc_id: classify(v).role for v in views(corpus) if v.doc_id not in corpus.excluded}


def priorities(corpus: Corpus) -> dict[str, int]:
    """Each read document's priority, from its classification's role."""
    return {d: 1 if r in P1_ROLES else 2 if r in P2_ROLES else 3 for d, r in roles(corpus).items()}


def _doc_chars(corpus: Corpus) -> dict[str, int]:
    chars: dict[str, int] = {}
    for block_id in corpus.order:
        block = corpus.blocks[block_id]
        chars[block.doc_id] = chars.get(block.doc_id, DOC_CHARS) + len(block_line(block)) + 1
    return chars


def _card_tokens_each(tile: Tile) -> int:
    empty = Block(id=f"{tile.id}-{CARD_BLOCKS}", doc_id=tile.doc_id, kind="tile_text", text="", page=tile.page)
    return _tokens(CARD_CHARS + CARD_BLOCKS * (len(block_line(empty)) + 1))


def _opening_open(corpus: Corpus, doc_id: str, tile_pages: set[int]) -> bool:
    """Whether card text can reach the document's opening text, which `selection.classify` reads: a tile on a page
    before the document's first page of text, or on that page while the text there is shorter than the opening."""
    own = [corpus.blocks[b] for b in corpus.all_order if corpus.blocks[b].doc_id == doc_id]
    pages = [b.page for b in own if b.page is not None]
    if not pages:
        return True
    first = min(pages)
    if min(tile_pages) < first:
        return True
    opening = [b.text for b in own if b.page == first][:HEADER_BLOCKS]
    return first in tile_pages and len(opening) < HEADER_BLOCKS and len(" ".join(opening)) < HEADER_CHARS


def card_tokens(
    corpus: Corpus, tiles: Iterable[Tile], priority: dict[str, int], core: Iterable[str] = ()
) -> tuple[int, int]:
    """At most how many tokens the page cards of `tiles` add to P1, and to its core (the doc ids in `core`), before any
    is read: a full card for each tile of a P1 document. A document no signal classifies yet (a scan under a plain
    name) whose opening text its cards can still reach may be classified by them, even as the AF, so it counts in both
    with its own text and a full card for each of its tiles. Excluded documents never enter P1."""
    by_doc: dict[str, list[Tile]] = {}
    for tile in tiles:
        if tile.doc_id not in corpus.excluded:
            by_doc.setdefault(tile.doc_id, []).append(tile)
    if not by_doc:
        return 0, 0
    core = set(core)
    unknown = {v.doc_id for v in views(corpus) if v.doc_id in by_doc and classify(v).discipline == "unknown"}
    sizes = _doc_chars(corpus)
    p1 = in_core = 0
    for doc_id, doc_tiles in by_doc.items():
        cards = sum(_card_tokens_each(t) for t in doc_tiles)
        if priority.get(doc_id, 3) == 1:
            p1 += cards
            in_core += cards if doc_id in core else 0
        elif doc_id in unknown and _opening_open(corpus, doc_id, {t.page for t in doc_tiles}):
            cards += _tokens(sizes.get(doc_id, DOC_CHARS))
            p1, in_core = p1 + cards, in_core + cards
    return p1, in_core


def _demotion_order(sizes: dict[str, int], prio: dict[str, int], role: dict[str, str | None], budget: int) -> list[str]:
    """P3, then P2, then P1 without its core, the largest first within each; a small time plan last."""

    def level(n: int) -> list[str]:
        return sorted((d for d in sizes if prio.get(d, 3) == n), key=lambda d: (-sizes[d], d))

    p1 = [d for d in level(1) if role.get(d) not in CORE_ROLES]
    small = [d for d in p1 if role.get(d) == "time_plan" and sizes[d] <= budget * SMALL_TIME_PLAN]
    return [*level(3), *level(2), *(d for d in p1 if d not in small), *small]


def plan_prefix(
    corpus: Corpus, calc: list[CalcPost], budget: PrefixBudget, cards_for: Iterable[Tile] = ()
) -> PrefixPlan:
    """What stays in full and what becomes search-only. Deterministic: sizes from characters, ties by doc id.
    `cards_for` are the tiles whose cards are still to be read: before the cards, the core must fit with the most text
    they can add (`card_tokens`)."""
    p0 = _tokens(prefix_chars(system_blocks(replace(corpus, order=[]), calc)) + MARK_CHARS)  # all but document text
    chars = _doc_chars(corpus)
    role = roles(corpus)
    prio = {d: 1 if r in P1_ROLES else 2 if r in P2_ROLES else 3 for d, r in role.items()}
    boundary_ids = [b for b in corpus.order if corpus.blocks[b].doc_id in corpus.excluded]
    sizes = {d: _tokens(n) for d, n in chars.items() if d not in corpus.excluded}
    by = {k: sum(t for d, t in sizes.items() if prio.get(d, 3) == k) for k in (1, 2, 3)}
    core_ids = [d for d in sizes if prio.get(d, 3) == 1 and role.get(d) in CORE_ROLES]
    tags = DOC_CHARS * len({corpus.blocks[b].doc_id for b in boundary_ids})
    boundary = _tokens(tags + sum(len(block_line(corpus.blocks[b])) + 1 for b in boundary_ids))
    cards, core_cards = card_tokens(corpus, cards_for, prio, core_ids)
    core = sum(sizes[d] for d in core_ids)
    plan = PrefixPlan(
        budget.tokens, p0, by[1], by[2], by[3], boundary, prio, cards=cards, core=core, core_cards=core_cards
    )
    if plan.stops:
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
    for doc_id in _demotion_order(sizes, prio, role, budget.tokens):
        if total <= budget.tokens:
            break
        total -= sizes[doc_id] - outline.get(doc_id, 0)
        plan.search_only.append(doc_id)
        if prio.get(doc_id) == 1:
            plan.p1_search_only.append(doc_id)
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
    p1 = set(plan.p1_search_only)
    why = {d: SEARCH_ONLY_P1 if d in p1 else SEARCH_ONLY for d in plan.search_only}
    return replace(corpus, order=order, search_only={**corpus.search_only, **why})


def p1_search_only(corpus: Corpus) -> list[str]:
    """The P1 documents the plan demoted, in document order: their text was reached by search, not held in full."""
    return [d for d, why in corpus.search_only.items() if why == SEARCH_ONLY_P1]


def fit_prefix(corpus: Corpus, calc: list[CalcPost], budgets: dict[str, Any]) -> tuple[Corpus, PrefixPlan | None]:
    """The planned corpus, or the corpus as it is when config sets no budget. Raises when the core doesn't fit."""
    budget = PrefixBudget.from_config(budgets)
    if budget is None:
        return corpus, None
    plan = plan_prefix(corpus, calc, budget)
    if plan.stops:
        raise PrefixOverBudget(over_budget(plan))
    return apply_plan(corpus, plan), plan


def over_budget(plan: PrefixPlan) -> str:
    cards = f" with at most {plan.core_cards} tokens of their page cards," if plan.core_cards else ""
    return (
        f"the prefix's fixed part ({plan.p0} tokens) and its priority-1 core, the AF and the contract ({plan.core} "
        f"tokens{cards}), exceed the budget of {plan.budget} tokens"
    )
