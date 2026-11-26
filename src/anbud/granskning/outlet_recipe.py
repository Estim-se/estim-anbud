"""Outlets per room type: when the documents call up a standard for how many outlets or points each room type gets,
compare the calc's recipe per room type with the customer's own recipe.

- Trigger: the documents' passage that calls up the standard (a lens may cite it with claim type `outlet_recipe`;
  this check also looks for it). The standard is only ever named by the code and clause that block itself writes.
  No standard's text or table is in the repo or in any prompt.
- Recipe: a cheap check role (Sonnet) maps calc posts to room types and item kinds and reads the number of rooms of
  each type (apartments per type times rooms per apartment) from the documents, with quotes code verifies. Code
  computes every count from the posts.
- Reference: the customer's own recipe, `<data>/<company>/references/outlet-recipe.toml`, with a required `source`
  ("entered by customer" or "extracted from customer calc <tender id>"). Only code reads its values; only its room
  type and item kind names go into the prompt, as the words to map onto.
- Pricing: the shortfall per room type times the rooms of that type is a quantity gap per item kind, priced by
  kronor. Without a customer recipe the check gives one unpriced flag citing the trigger, with the calc's recipe in
  the claim, so the estimator can compare by hand.
- `extract_recipe` turns a run's calc recipe into a customer recipe; only `anbud recipe-extract` writes it.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
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
from anbud.granskning.contracts import CalcPost, LensResult, ToolCall
from anbud.granskning.corpus import Corpus
from anbud.granskning.loop import SubmitOutcome
from anbud.granskning.toolbox import Toolbox
from anbud.model_client import ModelClient
from anbud.models import Citation, Flag, MoneyBasis
from anbud.paths import company_dir
from anbud.prep.text import normalise

CHECK_ID = "check_outlet_recipe"
CLAIM_TYPE = "outlet_recipe"
RECIPE_FILE = "outlet-recipe.toml"
SOURCES = ("entered by customer", "extracted from customer calc ")
MAX_TURNS = 8
MAX_USD = 0.50

DEFAULT_ROOMS = ("hall", "kök", "vardagsrum", "sovrum", "badrum", "wc", "klädkammare", "förråd", "balkong")
DEFAULT_KINDS = ("uttag", "dubbeluttag", "ljuspunkt", "strömbrytare", "data", "tv")

# A trigger candidate names a standard (the word, or a code-shaped reference) with outlet or room-type words in it or
# in the two blocks either side.
_STANDARD = re.compile(r"\bstandard\b|\b[A-ZÅÄÖ]{2,6}[\s-]?\d{2,4}(?:[\s-]\d{2,4}){1,3}\b")
_OUTLETS = re.compile(r"uttag|ljuspunkt|rumstyp|per\s+rum|varje\s+rum", re.IGNORECASE)


class RecipeError(ValueError):
    pass


# The customer's recipe


@dataclass(frozen=True)
class Recipe:
    source: str
    rooms: dict[str, dict[str, float]]  # room type -> item kind -> minimum count, keys normalised


def key(text: str) -> str:
    return " ".join(normalise(text).casefold().split())


def recipe_path(company_id: str, root: Path | None = None) -> Path:
    return company_dir(company_id, root) / "references" / RECIPE_FILE


def parse_recipe(text: str) -> Recipe:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RecipeError("the recipe isn't valid TOML") from exc
    source = data.get("source")
    if not isinstance(source, str) or not source.startswith(SOURCES):
        raise RecipeError('the recipe needs source = "entered by customer" or "extracted from customer calc <id>"')
    rooms = data.get("rooms")
    if not isinstance(rooms, dict) or not rooms:
        raise RecipeError("the recipe needs a [rooms.<room type>] table per room type")
    out: dict[str, dict[str, float]] = {}
    for room, kinds in rooms.items():
        if not isinstance(kinds, dict):
            raise RecipeError("each room type maps item kinds to counts")
        for kind, count in kinds.items():
            if isinstance(count, bool) or not isinstance(count, int | float) or count < 0:
                raise RecipeError("counts are numbers, 0 or more")
            out.setdefault(key(room), {})[key(kind)] = float(count)
    return Recipe(source, out)


def load_recipe(company_id: str, root: Path | None = None) -> Recipe | None:
    path = recipe_path(company_id, root)
    if not path.exists():
        return None
    return parse_recipe(path.read_text(encoding="utf-8"))


def _toml_key(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def recipe_toml(rooms: dict[str, dict[str, float]], source: str) -> str:
    lines = [
        "# The customer's outlet recipe per room type: minimum count per item kind. Read by code only.",
        f"source = {_toml_key(source)}",
    ]
    for room in sorted(rooms):
        lines += ["", f"[rooms.{_toml_key(room)}]"]
        lines += [f"{_toml_key(kind)} = {count:g}" for kind, count in sorted(rooms[room].items())]
    return "\n".join(lines) + "\n"


# The check's answer


class _Trigger(Answer):
    block_id: str
    quote: str
    standard: str | None = None
    clause: str | None = None


class _RoomCount(Answer):
    room_type: str
    apartment_type: str = ""
    apartments: float = Field(gt=0)
    per_apartment: float = Field(gt=0)
    block_id: str
    quote: str


class _Mapping(Answer):
    post_id: str
    room_type: str
    item_kind: str
    per: Literal["room", "total"]


class _RecipeAnswer(Answer):
    trigger: _Trigger | None = None
    rooms: list[_RoomCount] = Field(default_factory=list)
    mapping: list[_Mapping] = Field(default_factory=list)


class RecipeRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    room_type: str
    item_kind: str
    calc_count: float | None = Field(default=None, description="Per room, from the calc posts; None if unknown")
    posts: list[str] = Field(default_factory=list)
    recipe_count: float | None = None
    rooms: float | None = None
    gap_per_room: float | None = None
    gap_quantity: float | None = None


class RecipeSection(BaseModel):
    """The check's result for the report and for `anbud recipe-extract`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["no_trigger", "no_recipe", "compared", "failed"]
    trigger_block_id: str | None = None
    trigger_quote: str | None = None
    trigger_where: str = ""
    reference: str = Field(default="", description="The standard's code and clause as the trigger block writes them")
    recipe_source: str | None = None
    rooms: dict[str, float] = Field(default_factory=dict, description="Rooms per room type, from the documents")
    room_citations: dict[str, list[Citation]] = Field(default_factory=dict)
    rows: list[RecipeRow] = Field(default_factory=list)


def _in_block(text: str | None, block_text: str) -> bool:
    return bool(text) and key(text or "") in key(block_text)


def _number_in(value: float, quote: str) -> bool:
    shown = f"{value:g}"
    return re.search(rf"(?<![\d,.]){re.escape(shown)}(?![\d])", normalise(quote)) is not None


class _Answer:
    def __init__(self, corpus: Corpus, posts: dict[str, CalcPost]) -> None:
        self.corpus = corpus
        self.posts = posts
        self.trigger: _Trigger | None = None
        self.rooms: list[_RoomCount] = []
        self.mapping: list[_Mapping] = []
        self.answered = False

    def __call__(self, text: str, log: list[ToolCall], final: bool) -> SubmitOutcome:
        answer, problems = parse_answer(text, _RecipeAnswer)
        trigger, rooms, mapping = None, [], []
        if answer is not None:
            if answer.trigger is not None:
                t = answer.trigger
                problem = cite_problem(self.corpus, Cite(block_id=t.block_id, quote=t.quote), "trigger")
                if problem is None:
                    block = self.corpus.blocks[t.block_id].text
                    if t.standard and not _in_block(t.standard, block):
                        problems.append("trigger: the standard's code isn't written that way in the block")
                        t = t.model_copy(update={"standard": None})
                    if t.clause and not _in_block(t.clause, block):
                        problems.append("trigger: the clause isn't written that way in the block")
                        t = t.model_copy(update={"clause": None})
                    trigger = t
                else:
                    problems.append(problem)
            for n, room in enumerate(answer.rooms, 1):
                problem = cite_problem(self.corpus, Cite(block_id=room.block_id, quote=room.quote), f"rooms {n}")
                if problem is None and not _number_in(room.apartments, room.quote):
                    problem = f"rooms {n}: the quote doesn't contain the number of apartments"
                if problem is not None:
                    problems.append(problem)
                    continue
                rooms.append(room)
            for n, item in enumerate(answer.mapping, 1):
                if item.post_id not in self.posts:
                    problems.append(f"mapping {n}: post {item.post_id} is not in the calc")
                    continue
                mapping.append(item)
        if problems and not final:
            return SubmitOutcome(False, repair_text(problems))
        self.trigger, self.rooms, self.mapping, self.answered = trigger, rooms, mapping, answer is not None
        return SubmitOutcome(True, "Received.")


def trigger_hints(corpus: Corpus, lens_results: Sequence[LensResult] = ()) -> list[str]:
    """Blocks that may call up a standard for outlets per room type: lens flags of claim type `outlet_recipe` first,
    then blocks that name a standard with outlet or room-type words within two blocks. Empty: the check is skipped."""
    hints: dict[str, None] = {}
    for result in lens_results:
        for flag in result.flags:
            if flag.claim_type == CLAIM_TYPE:
                hints.update((c.block_id, None) for c in flag.citations)
    for block_id in corpus.order:
        if _STANDARD.search(corpus.blocks[block_id].text) and any(
            _OUTLETS.search(b.text) for b in corpus.neighbours(block_id, 2)
        ):
            hints[block_id] = None
    return list(hints)


def recipe_message(hints: list[str], rooms: Sequence[str], kinds: Sequence[str]) -> str:
    return f"""\
<task>
Check the outlet recipe per room type.

1. Trigger: find the passage in the tender documents that calls up a standard (or another named reference) for how \
many outlets or points each room type gets. Candidate blocks: {", ".join(hints[:30])}. Give its block id, an exact \
quote, and the standard's code and the clause exactly as that block writes them (null if it gives none). If no \
passage calls up such a reference, set "trigger" to null.
2. Rooms: from the documents' apartment and room data, give per room type and apartment type the number of apartments \
and how many rooms of that type each apartment has, each with a block id and an exact quote that contains the number \
of apartments.
3. Mapping: map every calc post that prices outlets, points, switches, data or TV in apartments to a room type and an \
item kind, and say whether the post's quantity is per room ("room") or the total for all rooms of that type ("total").

Room types: {", ".join(rooms)}. Item kinds: {", ".join(kinds)}. Use these words exactly; add another only if the calc \
clearly has one that none of them fits. Code computes every count; you only map and cite.

Answer with one JSON object in a ```json block, and no tool call:
{{"trigger": {{"block_id": "...", "quote": "...", "standard": "...", "clause": "..."}}, "rooms": [{{"room_type": \
"...", "apartment_type": "...", "apartments": 1, "per_apartment": 1, "block_id": "...", "quote": "..."}}], \
"mapping": [{{"post_id": "...", "room_type": "...", "item_kind": "...", "per": "room"}}]}}
</task>"""


def calc_recipe(
    mapping: Sequence[_Mapping], posts: dict[str, CalcPost], rooms: dict[str, float]
) -> dict[tuple[str, str], tuple[float | None, list[str]]]:
    """(room type, item kind) -> (count per room, post ids). A total is divided by the rooms of that type; without a
    room count it is unknown (None)."""
    out: dict[tuple[str, str], tuple[float | None, list[str]]] = {}
    for item in mapping:
        cell = (key(item.room_type), key(item.item_kind))
        count, ids = out.get(cell, (0.0, []))
        quantity = posts[item.post_id].quantity or 0.0
        if item.per == "room":
            added: float | None = quantity
        else:
            n = rooms.get(cell[0])
            added = quantity / n if n else None
        count = None if count is None or added is None else count + added
        out[cell] = (count, [*ids, item.post_id])
    return out


def room_totals(rooms: Sequence[_RoomCount]) -> tuple[dict[str, float], dict[str, list[Citation]]]:
    totals: dict[str, float] = {}
    cites: dict[str, list[Citation]] = {}
    seen: set[tuple[str, str]] = set()
    for room in rooms:
        ident = (key(room.room_type), key(room.apartment_type))
        if ident in seen:
            continue
        seen.add(ident)
        totals[ident[0]] = totals.get(ident[0], 0.0) + room.apartments * room.per_apartment
        cites.setdefault(ident[0], []).append(Citation(block_id=room.block_id, quote=room.quote))
    return totals, cites


def compare(
    cells: dict[tuple[str, str], tuple[float | None, list[str]]], rooms: dict[str, float], recipe: Recipe | None
) -> list[RecipeRow]:
    wanted = {(r, k): v for r, kinds in (recipe.rooms if recipe else {}).items() for k, v in kinds.items()}
    rows = []
    for cell in sorted(set(cells) | set(wanted)):
        count, ids = cells.get(cell, (0.0, []))
        target = wanted.get(cell)
        n = rooms.get(cell[0])
        gap = max(0.0, target - count) if target is not None and count is not None else None
        rows.append(
            RecipeRow(
                room_type=cell[0],
                item_kind=cell[1],
                calc_count=count,
                posts=ids,
                recipe_count=target,
                rooms=n,
                gap_per_room=gap,
                gap_quantity=gap * n if gap is not None and n else None,
            )
        )
    return rows


def extract_recipe(section: RecipeSection) -> dict[str, dict[str, float]]:
    """The calc's recipe per room type as a customer recipe: every count code could compute, rounded to 0.01."""
    rooms: dict[str, dict[str, float]] = {}
    for row in section.rows:
        if row.posts and row.calc_count is not None:
            rooms.setdefault(row.room_type, {})[row.item_kind] = round(row.calc_count, 2)
    return rooms


def _n(value: float | None) -> str:
    return "okänt" if value is None else f"{value:g}"


def _recipe_text(rows: Sequence[RecipeRow]) -> str:
    parts = [f"{r.room_type}: {r.item_kind} {_n(r.calc_count)}" for r in rows if r.posts]
    return "; ".join(parts) if parts else "inga kalkylposter kunde knytas till rumstyper"


def recipe_flags(section: RecipeSection, posts: dict[str, CalcPost]) -> list[Flag]:
    if section.status not in ("no_recipe", "compared") or section.trigger_block_id is None:
        return []
    trigger = Citation(block_id=section.trigger_block_id, quote=section.trigger_quote or "")
    ref = section.reference or "en standard"
    if section.status == "no_recipe":
        claim = (
            f"Handlingarna hänvisar till {ref} för antal uttag per rumstyp. Kundens recept finns inte inlagt, så "
            f"jämför kalkylens recept per rum för hand: {_recipe_text(section.rows)}."
        )
        basis = MoneyBasis(kind="quantity_gap", item="uttag per rumstyp")  # no quantity: unpriced, a check
        return [Flag(lens_id=CHECK_ID, claim_type=CLAIM_TYPE, claim=claim, citations=[trigger], money_basis=basis)]
    flags = []
    by_kind: dict[str, list[RecipeRow]] = {}
    for row in section.rows:
        if row.gap_per_room:
            by_kind.setdefault(row.item_kind, []).append(row)
    for kind, rows in sorted(by_kind.items()):
        parts = [
            f"{r.room_type}: kundens recept anger {_n(r.recipe_count)}, kalkylen har {_n(r.calc_count)}" for r in rows
        ]
        claim = f"{kind} – " + "; ".join(parts) + f" (FU hänvisar till {ref})."
        priced = [r.gap_quantity for r in rows if r.gap_quantity is not None]
        quantity = sum(priced) if priced else None
        citations = [trigger]
        for r in rows:
            citations += [c for c in section.room_citations.get(r.room_type, []) if c not in citations]
        post_ids = [p for r in rows for p in r.posts if p in posts]
        priced_post = next((p for p in post_ids if posts[p].unit_price or posts[p].amount), None) or next(
            iter(post_ids), None
        )
        if priced_post is not None:
            basis = MoneyBasis(kind="quantity_gap", post_ref=priced_post, item=kind, quantity=quantity, unit="st")
        else:
            basis = MoneyBasis(kind="missing_post", item=kind, quantity=quantity, unit="st")
        flags.append(Flag(lens_id=CHECK_ID, claim_type=CLAIM_TYPE, claim=claim, citations=citations, money_basis=basis))
    return flags


def check_outlet_recipe(
    client: ModelClient,
    *,
    corpus: Corpus,
    calc: list[CalcPost],
    system: list[dict[str, Any]],
    toolbox: Toolbox,
    recipe: Recipe | None,
    lens_results: Sequence[LensResult] = (),
) -> tuple[RecipeSection, list[Flag], CheckRun]:
    run = CheckRun(CHECK_ID)
    hints = trigger_hints(corpus, lens_results)
    if not hints:
        run.skipped = "ingen hänvisning till en standard för uttag per rumstyp hittades i handlingarna"
        return RecipeSection(status="no_trigger"), [], run
    posts = {p.id: p for p in calc}
    rooms = list(recipe.rooms) if recipe else list(DEFAULT_ROOMS)
    kinds = sorted({k for v in recipe.rooms.values() for k in v}) if recipe else list(DEFAULT_KINDS)
    handler = _Answer(corpus, posts)
    run.outcomes.append(
        run_check(
            client,
            step=CHECK_ID,
            system=system,
            message=recipe_message(hints, rooms, kinds),
            toolbox=toolbox,
            handle=handler,
            max_turns=MAX_TURNS,
            max_usd=MAX_USD,
        )
    )
    if not handler.answered:
        return RecipeSection(status="failed"), [], run
    if handler.trigger is None:
        return RecipeSection(status="no_trigger"), [], run
    totals, room_cites = room_totals(handler.rooms)
    cells = calc_recipe(handler.mapping, posts, totals)
    t = handler.trigger
    section = RecipeSection(
        status="compared" if recipe else "no_recipe",
        trigger_block_id=t.block_id,
        trigger_quote=t.quote,
        trigger_where=block_where(corpus, t.block_id),
        reference=" ".join(x for x in (t.standard, t.clause) if x),
        recipe_source=recipe.source if recipe else None,
        rooms=totals,
        room_citations=room_cites,
        rows=compare(cells, totals, recipe),
    )
    return section, recipe_flags(section, posts), run
