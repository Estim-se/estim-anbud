"""Zero and "not included" calc rows: every one gets the tender block that explains it, or a plain "not found".

- Code picks the rows: a typed post with quantity 0 or amount 0, an untyped row whose values are all 0 or that has a 0
  under a quantity or amount label, or text that says the work isn't included (ingår ej, ej ingående, exkl. and
  similar; "exkl. moms" is a price note, not an exclusion).
- A cheap check role (Sonnet) gets them in batches and searches the documents. Per row it answers explained (the
  documents put the work outside this contract), contradicted (the documents say it IS included) or not found, with a
  block id and a quote that code verifies. A quote that fails after the repair turn counts as not found.
- A contradicted row becomes a flag: a missing post on that calc row, unpriced unless kronor can price it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from anbud.granskning.calc import as_number, header_role
from anbud.granskning.check_loop import (
    STOP_NAMES,
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
from anbud.granskning.prefix import post_line
from anbud.granskning.toolbox import Toolbox
from anbud.model_client import ModelClient
from anbud.models import Citation, Flag, MoneyBasis

CHECK_ID = "check_zero_rows"
BATCH = 20
MAX_TURNS = 5
MAX_USD = 0.30
NOT_FOUND = "ingen hänvisning hittad i handlingarna"

_EXCLUDED = re.compile(
    r"\b(?:ingår|ingående|medräknad|medräknat|medräknas)\s+(?:ej|inte)\b"
    r"|\b(?:ej|inte|icke)\s+(?:ingående|medräknad|medräknat|inräknad|inräknat|i\s+anbudet)\b"
    r"|\bexkl(?:usive)?\b(?!\.?\s*(?:moms|mervärdesskatt|vat)\b)|\butanför\s+(?:entreprenaden|anbudet)\b",
    re.IGNORECASE,
)

_ZERO_ROLES = {"quantity", "amount", "unit_price", "hours"}

Status = Literal["explained", "contradicted", "not_found"]


class ZeroRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    post_id: str
    sheet: str | None = None
    row: int | None = None
    text: str
    why: str  # why code picked the row, in Swedish
    status: Status = "not_found"
    block_id: str | None = None
    quote: str | None = None
    where: str = ""
    note: str = ""


def _untyped(post: CalcPost) -> bool:
    return all(v is None for v in (post.quantity, post.unit_price, post.amount, post.hours))


def _untyped_zero(text: str) -> bool:
    """An untyped row ("label: Antal: 0 | Summa: 0") is a zero row when every value is 0, or when a 0 sits under a
    label that reads as a quantity, amount, price or hours header. A 0 in a matrix of unnamed columns is not."""
    values = []
    for part in text.split(": ", 1)[-1].split(" | "):
        label, _, value = part.rpartition(": ")
        number = as_number(value)
        if number is not None:
            values.append((label, number))
    if not values:
        return False
    if all(v == 0 for _, v in values):
        return True
    return any(v == 0 and header_role(label) in _ZERO_ROLES for label, v in values)


def zero_reason(post: CalcPost) -> str | None:
    """Why a post is a zero or not-included row, or None."""
    reasons = []
    if post.quantity == 0:
        reasons.append("antal 0")
    if post.amount == 0:
        reasons.append("belopp 0")
    if _untyped(post) and _untyped_zero(post.text):
        reasons.append("noll i raden")
    if _EXCLUDED.search(f"{post.section or ''} {post.text}"):
        reasons.append("texten säger att det inte ingår")
    return ", ".join(reasons) if reasons else None


def zero_rows(calc: Sequence[CalcPost]) -> list[ZeroRow]:
    rows = []
    for post in calc:
        why = zero_reason(post)
        if why is not None:
            rows.append(ZeroRow(post_id=post.id, sheet=post.sheet, row=post.row, text=post.text, why=why))
    return rows


class _Entry(Answer):
    post_id: str
    status: Status
    block_id: str | None = None
    quote: str | None = None
    note: str | None = None


class _ZeroAnswer(Answer):
    posts: list[_Entry]


def zero_message(posts: Sequence[CalcPost]) -> str:
    lines = "\n".join(post_line(p) for p in posts)
    return f"""\
<task>
Check calc rows that are zero or marked as not included. For each row below, search the tender documents for the \
block that explains why the contractor leaves it out or prices it at zero (another trade does it, the client supplies \
it, it is an option, the AF excludes it, it isn't needed), or a block that says the work IS part of the electrical \
contract.

Then answer with one JSON object in a ```json block, and no tool call:
{{"posts": [{{"post_id": "...", "status": "explained", "block_id": "...", "quote": "...", "note": "..."}}]}}
- status "explained": the documents put the work outside this contract or make it unnecessary.
- status "contradicted": the documents say the work is part of this contract.
- status "not_found": nothing in the documents addresses it; leave out block_id and quote.
One entry per row. Quotes are copied exactly from the block, one short sentence; code checks every quote. The note \
is one sentence in Swedish.
</task>
<rows>
{lines}
</rows>"""


class _Batch:
    def __init__(self, corpus: Corpus, posts: Sequence[CalcPost]) -> None:
        self.corpus = corpus
        self.ids = {p.id for p in posts}
        self.entries: dict[str, _Entry] = {}

    def __call__(self, text: str, log: list[ToolCall], final: bool) -> SubmitOutcome:
        answer, problems = parse_answer(text, _ZeroAnswer)
        good: dict[str, _Entry] = {}
        if answer is not None:
            for entry in answer.posts:
                if entry.post_id not in self.ids:
                    problems.append(f"post {entry.post_id} is not one of the rows asked about")
                    continue
                if entry.status != "not_found":
                    cite = Cite(block_id=entry.block_id or "", quote=entry.quote or "")
                    problem = cite_problem(self.corpus, cite, f"post {entry.post_id}")
                    if problem is not None:
                        problems.append(problem)
                        continue
                good[entry.post_id] = entry
            missing = sorted(self.ids - {e.post_id for e in answer.posts})
            if missing:
                problems.append("rows with no entry: " + ", ".join(missing))
        if problems and not final:
            return SubmitOutcome(False, repair_text(problems))
        self.entries = good
        return SubmitOutcome(True, "Received.")


def _flag(row: ZeroRow) -> Flag:
    note = f" {row.note}" if row.note else ""
    return Flag(
        lens_id=CHECK_ID,
        claim_type="unpriced_requirement",
        claim=f"Kalkylraden «{row.text}» är noll eller tas inte med, men handlingarna anger att arbetet ingår.{note}",
        citations=[Citation(block_id=row.block_id or "", quote=row.quote or "")],
        money_basis=MoneyBasis(kind="missing_post", post_ref=row.post_id, item=row.text),
        check_key=f"{CHECK_ID}:{row.post_id}",
    )


def check_zero_rows(
    client: ModelClient,
    *,
    corpus: Corpus,
    calc: Sequence[CalcPost],
    system: list[dict[str, Any]],
    toolbox: Toolbox,
    batch: int = BATCH,
) -> tuple[list[ZeroRow], list[Flag], CheckRun]:
    """The zero rows with their status and citation, the flags for contradicted rows, and the check's loops."""
    rows = zero_rows(calc)
    run = CheckRun(CHECK_ID)
    if not rows:
        run.skipped = "kalkylen har inga nollrader eller ingår ej-rader"
        return [], [], run
    posts = {p.id: p for p in calc}
    found: dict[str, _Entry] = {}
    for n, start in enumerate(range(0, len(rows), batch), 1):
        chunk = [posts[r.post_id] for r in rows[start : start + batch]]
        handler = _Batch(corpus, chunk)
        outcome = run_check(
            client,
            step=f"{CHECK_ID}/b{n}",
            system=system,
            message=zero_message(chunk),
            toolbox=toolbox,
            handle=handler,
            max_turns=MAX_TURNS,
            max_usd=MAX_USD,
        )
        run.outcomes.append(outcome)
        found.update(handler.entries)
        if outcome.error in STOP_NAMES:
            break
    out = []
    for row in rows:
        entry = found.get(row.post_id)
        if entry is None:
            out.append(row.model_copy(update={"note": NOT_FOUND}))
            continue
        update: dict[str, Any] = {"status": entry.status, "note": entry.note or ""}
        if entry.status != "not_found":
            update |= {"block_id": entry.block_id, "quote": entry.quote, "where": block_where(corpus, entry.block_id)}
        else:
            update["note"] = entry.note or NOT_FOUND
        out.append(row.model_copy(update=update))
    flags = [_flag(r) for r in out if r.status == "contradicted"]
    return out, flags, run
