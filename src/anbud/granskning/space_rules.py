"""Per-space rules: when the documents set a count per space for a list of space types ("one outlet in each storage
room, technical room and corridor"), the quantity must follow the spaces that exist.

- A cheap check role (Sonnet) finds each rule: the item, the count per space, the space types and the rule's block.
  It then lists every qualifying space, each with a block id and a quote naming it (room schedules, room lists,
  drawing text from page cards), and maps the calc post that prices the item, if any.
- Code verifies every quote, counts distinct spaces, computes spaces times count per space, and compares it with the
  calc post's quantity. A shortfall becomes a quantity gap on that post (or a missing post) that kronor prices. A
  mapped post with no quantity is "unknown": shown in the table, never priced as if it had 0.
- Each rule is its own check: a rule for outlets is checked even when a lens flagged lighting in the same spaces.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from anbud.granskning.check_loop import (
    Answer,
    CheckRun,
    Cite,
    block_where,
    cite_problem,
    parse_answer,
    repair_text,
    run_check,
)
from anbud.granskning.contracts import CalcPost, ToolCall
from anbud.granskning.corpus import Corpus
from anbud.granskning.loop import SubmitOutcome
from anbud.granskning.toolbox import Toolbox
from anbud.model_client import ModelClient
from anbud.models import Citation, Flag, MoneyBasis
from anbud.prep.text import normalise

CHECK_ID = "check_space_rules"
MAX_TURNS = 8
MAX_USD = 0.50

MESSAGE = """\
<task>
Find rules in the tender documents that set a count per space for a list of space types, typically for common areas: \
for example one outlet, one luminaire or one detector in each storage room, technical room, corridor or stairwell. \
A rule counts only if it names the item, a count per space and the space types it covers.

For each rule:
1. Cite the rule (block id and an exact quote) and give the item, the count per space and the space types.
2. List every space in the documents that the rule covers, from room schedules, room lists, descriptions or drawing \
text (blocks of kind I are text read from drawing tiles). Each space gets the block id and an exact quote that names \
it. One entry per space; never list the same space twice.
3. Give the id of the calc post that prices this item for these spaces (from <calculation>), or null if there is none.

Every rule is a separate entry, even when two rules cover the same spaces. If the documents have no such rule, answer \
with an empty list. Search with several words (the space types, the item, "per", "varje", "samtliga").

Answer with one JSON object in a ```json block, and no tool call:
{"rules": [{"item": "...", "per_space": 1, "space_types": ["..."], "rule": {"block_id": "...", "quote": "..."}, \
"post_id": null, "spaces": [{"name": "...", "block_id": "...", "quote": "..."}]}]}
Quotes are copied exactly from the block; code checks every one and counts the spaces itself.
</task>"""


class _Space(Answer):
    name: str
    block_id: str
    quote: str


class _Rule(Answer):
    item: str
    per_space: float = Field(gt=0)
    space_types: list[str] = Field(default_factory=list)
    rule: Cite
    post_id: str | None = None
    spaces: list[_Space] = Field(default_factory=list)


class _RulesAnswer(Answer):
    rules: list[_Rule]


class SpaceRule(BaseModel):
    """One checked rule, for the report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    item: str
    per_space: float
    space_types: list[str]
    rule_block_id: str
    rule_quote: str
    rule_where: str = ""
    spaces: list[str] = Field(default_factory=list, description="The counted spaces' names")
    space_blocks: list[str] = Field(default_factory=list)
    required: float
    post_id: str | None = None
    calc_quantity: float | None = None
    gap: float | None = 0.0
    status: Literal["gap", "ok", "no_post", "unknown"]


def _space_key(space: _Space) -> str:
    return normalise(space.name).casefold()


class _Rules:
    def __init__(self, corpus: Corpus, posts: dict[str, CalcPost]) -> None:
        self.corpus = corpus
        self.posts = posts
        self.rules: list[tuple[_Rule, list[_Space]]] = []

    def __call__(self, text: str, log: list[ToolCall], final: bool) -> SubmitOutcome:
        answer, problems = parse_answer(text, _RulesAnswer)
        kept: list[tuple[_Rule, list[_Space]]] = []
        for n, rule in enumerate(answer.rules if answer else [], 1):
            problem = cite_problem(self.corpus, rule.rule, f"rule {n}")
            if problem is not None:
                problems.append(problem)
                continue
            if rule.post_id is not None and rule.post_id not in self.posts:
                problems.append(f"rule {n}: post {rule.post_id} is not in the calc")
                continue
            spaces: dict[str, _Space] = {}
            for m, space in enumerate(rule.spaces, 1):
                cite = Cite(block_id=space.block_id, quote=space.quote)
                problem = cite_problem(self.corpus, cite, f"rule {n}, space {m}")
                if problem is not None:
                    problems.append(problem)
                    continue
                spaces.setdefault(_space_key(space), space)
            kept.append((rule, list(spaces.values())))
        if problems and not final:
            return SubmitOutcome(False, repair_text(problems))
        self.rules = kept
        return SubmitOutcome(True, "Received.")


def _checked(corpus: Corpus, rule: _Rule, spaces: list[_Space], posts: dict[str, CalcPost]) -> SpaceRule:
    required = len(spaces) * rule.per_space
    post = posts.get(rule.post_id) if rule.post_id else None
    calc_quantity = post.quantity if post is not None else None
    gap: float | None
    if post is None:
        gap = required
        status = "no_post" if required > 0 else "ok"
    elif calc_quantity is None:  # the post has no quantity (untyped, or a lump sum): unknown, not 0
        gap, status = None, "unknown"
    else:
        gap = max(0.0, required - calc_quantity)
        status = "gap" if gap > 0 else "ok"
    return SpaceRule(
        item=rule.item,
        per_space=rule.per_space,
        space_types=rule.space_types,
        rule_block_id=rule.rule.block_id,
        rule_quote=rule.rule.quote,
        rule_where=block_where(corpus, rule.rule.block_id),
        spaces=[s.name for s in spaces],
        space_blocks=list(dict.fromkeys(s.block_id for s in spaces)),
        required=required,
        post_id=rule.post_id if post is not None else None,
        calc_quantity=calc_quantity,
        gap=gap,
        status=status,
    )


def _n(value: float) -> str:
    return f"{value:g}"


def _flag(checked: SpaceRule, rule: _Rule, spaces: list[_Space], posts: dict[str, CalcPost], n: int) -> Flag:
    citations = [Citation(block_id=rule.rule.block_id, quote=rule.rule.quote)]
    citations += [Citation(block_id=s.block_id, quote=s.quote) for s in spaces]
    types = ", ".join(checked.space_types)
    counted = f"Regeln ger {_n(checked.per_space)} {checked.item} per utrymme ({types}); handlingarna visar " + (
        f"{len(spaces)} sådana utrymmen, alltså {_n(checked.required)}."
    )
    if checked.status == "no_post":
        claim = f"{counted} Kalkylen har ingen post för det."
        basis = MoneyBasis(kind="missing_post", item=checked.item, quantity=checked.required, unit="st")
    else:
        post = posts[checked.post_id or ""]
        claim = f"{counted} Kalkylen har {_n(checked.calc_quantity or 0)}; det fattas {_n(checked.gap or 0)}."
        basis = MoneyBasis(
            kind="quantity_gap", post_ref=post.id, item=checked.item, quantity=checked.gap, unit=post.unit or "st"
        )
    return Flag(
        lens_id=CHECK_ID,
        claim_type="quantity_realism" if checked.status == "gap" else "unpriced_requirement",
        claim=claim,
        citations=citations,
        money_basis=basis,
        check_key=f"{CHECK_ID}:{n}:{normalise(checked.item).casefold()}",  # each rule priced on its own
    )


def check_space_rules(
    client: ModelClient,
    *,
    corpus: Corpus,
    calc: list[CalcPost],
    system: list[dict[str, Any]],
    toolbox: Toolbox,
) -> tuple[list[SpaceRule], list[Flag], CheckRun]:
    """Every rule found with its count and calc comparison, a flag per rule that falls short (none for a rule whose
    post has no quantity), and the check's loop."""
    posts = {p.id: p for p in calc}
    handler = _Rules(corpus, posts)
    run = CheckRun(CHECK_ID)
    run.outcomes.append(
        run_check(
            client,
            step=CHECK_ID,
            system=system,
            message=MESSAGE,
            toolbox=toolbox,
            handle=handler,
            max_turns=MAX_TURNS,
            max_usd=MAX_USD,
        )
    )
    checked: list[SpaceRule] = []
    flags: list[Flag] = []
    for n, (rule, spaces) in enumerate(handler.rules, 1):
        one = _checked(corpus, rule, spaces, posts)
        checked.append(one)
        if one.status in ("gap", "no_post"):
            flags.append(_flag(one, rule, spaces, posts, n))
    return checked, flags, run
