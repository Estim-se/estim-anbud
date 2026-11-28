"""The code-group pass: one targeted turn per code group of the technical description that nothing covers after the
extraction (`kalkyl.code_groups`), read twice.

The extraction answers marked lines; a group whose text the line finder never marked can be left with no record and
no set-aside. Each such group is sent on its own, with all its blocks, and the model answers one of two ways: the
priced work it requires, as records with citations (code then counts, applies the recipe and prices them like any
other record), or no priced work, with a set-aside kind and citation. Unclear is allowed only with what is missing.

The turn is answered twice, as fresh samples of the same request (`ModelClient.call(sample=...)`), with the extraction's
own system blocks and tools. Records go into the calc only where the two readings agree: a requirement of the first
reading is kept when every group block it cites was given the same kinds of requirement by the second (source, object
kind, scope, and the quantity when it is stated), and so is its set-aside of a block both readings set aside. A group
both readings call no priced work gets the first reading's set-asides. Anything else (records against no priced
work, an unclear answer, no answer, an error, or records that agree nowhere) leaves the group as a question on the
Kodgrupper sheet, with both readings, and keeps nothing. A sub-code of a group with records that no kept record or
set-aside covers is listed again, as read in its group. The AF is not read: its groups stay counts.

The sheet describes a reading by its kinds, labels and block ids; the one piece of model text it carries is what an
unclear reading says is missing. Off by default (config/kalkyl.toml `[group_pass]`, or `anbud kalkyl --group-pass`):
a rebuild of a run made without it then stays a complete $0 replay. Calls: `READINGS` per uncovered group.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from anbud.granskning.corpus import Corpus
from anbud.kalkyl.code_groups import TECHNICAL, CodeGroup, coverage
from anbud.kalkyl.extract import ChunkResult, _review, apply_tables, chunk_message
from anbud.kalkyl.lines import Chunk
from anbud.kalkyl.price import OBJECT_LABELS, SCOPE_LABELS, Post, totals_of
from anbud.kalkyl.records import SET_ASIDE_LABELS, Level, QuantityRecord, SetAside, Uncovered, VerifiedRecord
from anbud.kalkyl.settings import EXTRACT, RoleSettings
from anbud.kalkyl.tables import Cells
from anbud.model_client import BudgetExceeded, ModelClient, ModelError, ReplayMiss, RunStopped

READINGS = 2
PREFIX = "g-"  # a group chunk's id: g-<its first block's id>
RECORDS = "records"
NO_WORK = "no_priced_work"
QUESTION = "question"
UNCLEAR = "unclear"
NONE = "none"
ANSWERED = (RECORDS, NO_WORK, UNCLEAR, NONE)
OUTCOME_LABELS = {
    RECORDS: "Poster: båda läsningarna överens",
    NO_WORK: "Inget prissatt arbete: båda läsningarna överens",
    QUESTION: "Fråga till kalkylatorn: läsningarna är inte överens",
}
NOT_READ = "Inte läst av kodgruppsläsningen"
READ_IN = "Läst i kodgruppen {code}: ingen post och inget bortval som båda läsningarna gav här"
AF_NOT_READ = "AF: läses inte, bara räknas"
SOURCE_LABELS = {
    "stated": "angiven mängd",
    "per_object": "per",
    "list_row": "rad i lista",
    "lump_sum": "klumpsumma",
    "missing": "mängd saknas",
}
ASK = (
    "These blocks are one code group of the technical description, sent on their own: no record and no set-aside "
    "cites any of them yet. Account for every block of the group, its sub-code headings included. (a) Electrical "
    "work that costs money: record it as requirements with citations, as for any chunk; a scope passage gives one "
    "per_object requirement per item and object kind or room type it names, with a quantity only if one is written. "
    "(b) Text that asks for no priced work: set it aside with the kind that fits, also in a group that has priced "
    "work: no_requirement for a general requirement or information, other_trade, not_this_contract, "
    "heading_or_contents. Use unclear only when the text can't tell what is installed or who does it, and then say "
    "in the reason what is missing. Call submit_quantities once."
)


@dataclass(frozen=True)
class GroupReading:
    outcome: str  # one of ANSWERED, or why it wasn't read (an error kind, budget, stopped, replay_miss)
    label: str  # Swedish, for the Kodgrupper sheet
    kinds: dict[str, tuple[str, ...]] = field(default_factory=dict)  # block id -> the kinds of requirement citing it


@dataclass
class GroupResult:
    group: CodeGroup
    chunk: Chunk
    readings: list[GroupReading] = field(default_factory=list)
    outcome: str = QUESTION
    records: list[tuple[QuantityRecord, Level]] = field(default_factory=list)  # agreed: the first reading's
    set_aside: list[SetAside] = field(default_factory=list)  # agreed: the first reading's

    @property
    def read(self) -> bool:
        """Both readings came back, agreeing or not."""
        return len(self.readings) == READINGS and all(r.outcome in ANSWERED for r in self.readings)

    def chunk_result(self) -> ChunkResult:
        return ChunkResult(self.chunk.id, "submitted", records=list(self.records), set_aside=list(self.set_aside))


@dataclass
class GroupPass:
    groups: list[GroupResult] = field(default_factory=list)
    calls: int = 0
    cost_usd: float = 0.0
    halted: str | None = None  # budget or stopped: the groups after it were not read

    @property
    def set_aside_blocks(self) -> set[str]:
        return {b for g in self.groups for s in g.set_aside for b in s.block_ids}

    def by_code(self) -> dict[str, GroupResult]:
        return {g.group.code: g for g in self.groups}

    def chunk_results(self) -> list[ChunkResult]:
        """The agreed groups, as chunk results: their records go through the normal record path, and their set-asides
        of another trade's work give connection records like the extraction's (`connections`)."""
        return [g.chunk_result() for g in self.groups if g.records or g.set_aside]

    def summary(self, posts: list[Post] | None = None) -> dict[str, Any]:
        """Counts only: safe to print and to log. With the calc's posts: the posts the pass added (every line from a
        pass record) or extended, and the share of the added posts' total that rests on an assumed count."""
        out: dict[str, Any] = {
            "groups": len(self.groups),
            "to_records": sum(g.outcome == RECORDS for g in self.groups),
            "no_priced_work": sum(g.outcome == NO_WORK for g in self.groups),
            "questions": sum(g.outcome == QUESTION for g in self.groups),
            "not_read": sum(not g.read for g in self.groups),
            "readings_same_outcome": sum(g.read and len({r.outcome for r in g.readings}) == 1 for g in self.groups),
            "records_added": sum(len(g.records) for g in self.groups),
            "calls": self.calls,
            "cost_usd": round(self.cost_usd, 4),
            "halted": self.halted,
        }
        if posts is not None:
            mine = [p for p in posts if any(ln.record.chunk_id.startswith(PREFIX) for ln in p.lines)]
            added = [p for p in mine if all(ln.record.chunk_id.startswith(PREFIX) for ln in p.lines)]
            totals = totals_of(added)
            share = round(float(totals.assumed_total / totals.total), 4) if totals.total else 0.0
            out |= {
                "posts_added": len(added),
                "posts_extended": len(mine) - len(added),
                "posts_added_total_sek": float(totals.total),
                "posts_added_assumed_sek": float(totals.assumed_total),
                "posts_added_assumed_share": share,
            }
        return out


def marked_lines(chunks: list[Chunk]) -> dict[str, str]:
    out = {b: "quantity" for c in chunks for b in c.quantity_ids}
    out.update((b, "scope") for c in chunks for b in c.scope_ids)
    return out


def uncovered_groups(
    corpus: Corpus, verified: list[VerifiedRecord], uncovered: list[Uncovered], lines: set[str]
) -> list[CodeGroup]:
    """The topmost groups of the technical description with neither a requirement record nor a line set aside with a
    reason, after the extraction."""
    posted = {b for v in verified if v.record.record_kind == "requirement" for b in v.block_ids}
    reasoned = {u.block_id for u in uncovered if not u.unresolved}
    open_lines = {u.block_id for u in uncovered if u.unresolved}
    cov = coverage(corpus, posted, reasoned, lines, open_lines)
    return [g for g in cov.missing if g.section == TECHNICAL]


def bound(corpus: Corpus, paths: dict[str, tuple[str, ...]], cells: Cells | None) -> tuple[int, int]:
    """Before any reading, at most how many groups the pass reads and how many message characters it sends per
    reading: every code of the technical description could be a topmost uncovered group, and topmost groups never
    share a block, so the groups' blocks are at most the top groups' blocks."""
    cov = coverage(corpus, set(), set(), set(), set())
    groups = sum(g.section == TECHNICAL for g in cov.codes.values())
    tops = [g for g in cov.missing if g.section == TECHNICAL]
    chars = sum(len(chunk_message(group_chunk(g, {}), corpus, paths, cells)) for g in tops)
    overhead = len(ASK) + 400  # the tags and heading path of each message
    return groups, chars + groups * overhead


def group_chunk(group: CodeGroup, lines: dict[str, str]) -> Chunk:
    """The group's blocks as one chunk; its marked lines keep their marks."""
    ids = list(group.block_ids)
    return Chunk(
        id=f"{PREFIX}{ids[0] if ids else group.block_id}",
        doc_id=group.doc_id,
        block_ids=ids,
        quantity_ids=[b for b in ids if lines.get(b) == "quantity"],
        scope_ids=[b for b in ids if lines.get(b) == "scope"],
    )


def _kind(record: QuantityRecord) -> str:
    parts = [record.quantity_source, record.per_object or "", record.scope]
    if record.quantity_source == "stated" and record.quantity is not None:
        parts.append(f"{record.quantity:g}")
    return ":".join(parts)


def _record_label(record: QuantityRecord) -> str:
    source = SOURCE_LABELS.get(record.quantity_source, record.quantity_source)
    if record.per_object:
        source += f" {OBJECT_LABELS.get(record.per_object, (record.per_object,))[0]}"
    if record.quantity is not None:
        source += f" {record.quantity:g}"
    blocks = ", ".join(dict.fromkeys(c.block_id for c in record.citations))
    return f"{source}, {SCOPE_LABELS.get(record.scope, record.scope)} [{blocks}]"


def classify_reading(reading: ChunkResult, chunk: Chunk) -> GroupReading:
    """One reading's answer for its group."""
    mine = set(chunk.block_ids)
    work = [r for r, _ in reading.records if r.record_kind == "requirement"]
    if work:
        kinds: dict[str, set[str]] = {}
        for record in work:
            for c in record.citations:
                if c.block_id in mine:
                    kinds.setdefault(c.block_id, set()).add(_kind(record))
        label = f"Poster ({len(work)}): " + "; ".join(_record_label(r) for r in work)
        return GroupReading(RECORDS, label[:1000], {b: tuple(sorted(k)) for b, k in kinds.items()})
    asides = [s for s in reading.set_aside if set(s.block_ids) & mine]
    definite = [s for s in asides if s.kind != "unclear"]
    if definite:
        parts = [f"{SET_ASIDE_LABELS[s.kind]} [{', '.join(s.block_ids)}]" for s in definite]
        return GroupReading(NO_WORK, ("Inget prissatt arbete: " + "; ".join(parts))[:1000])
    if asides:
        parts = [f"[{', '.join(s.block_ids)}] {s.reason}" for s in asides]
        return GroupReading(UNCLEAR, ("Oklart, saknas: " + "; ".join(parts))[:1000])
    return GroupReading(NONE, "Inget svar för gruppen")


def _set_aside_agreed(readings: list[ChunkResult], mine: set[str]) -> list[SetAside]:
    """The first reading's definite set-asides, on the group blocks every reading set aside as no priced work (any
    kind but unclear) and no reading cites in a requirement."""

    def definite(r: ChunkResult) -> set[str]:
        cited = {c.block_id for rec, _ in r.records if rec.record_kind == "requirement" for c in rec.citations}
        return {b for s in r.set_aside if s.kind != "unclear" for b in s.block_ids if b in mine} - cited

    agreed = set.intersection(*(definite(r) for r in readings))
    out = []
    for item in readings[0].set_aside:
        keep = [b for b in item.block_ids if b in agreed]
        if item.kind != "unclear" and keep:
            out.append(item.model_copy(update={"block_ids": keep}))
    return out


def agree(result: GroupResult, readings: list[ChunkResult]) -> None:
    """The group's outcome from its readings. Both give records: the first reading's requirements whose cited blocks
    every reading gave the same kinds of requirement, and its set-asides of blocks every reading set aside; none kept
    is a question. Both say no priced work: the first reading's set-asides. Anything else: a question, with nothing
    kept, so the group stays listed as uncovered."""
    outcomes = {r.outcome for r in result.readings}
    mine = set(result.chunk.block_ids)
    if outcomes == {RECORDS}:
        kinds = [r.kinds for r in result.readings]
        for record, level in readings[0].records:
            if record.record_kind != "requirement":
                continue
            cited = {c.block_id for c in record.citations} & mine
            if cited and all(k.get(b) == kinds[0].get(b) for k in kinds[1:] for b in cited):
                result.records.append((record, level))
        if result.records:
            result.set_aside = _set_aside_agreed(readings, mine)
        result.outcome = RECORDS if result.records else QUESTION
    elif outcomes == {NO_WORK}:
        result.set_aside = [s for s in readings[0].set_aside if s.kind != "unclear" and set(s.block_ids) & mine]
        result.outcome = NO_WORK
    else:
        result.outcome = QUESTION


def read_groups(
    client: ModelClient,
    groups: list[CodeGroup],
    lines: dict[str, str],
    corpus: Corpus,
    catalog_ids: set[str],
    system: list[dict[str, Any]],
    paths: dict[str, tuple[str, ...]],
    settings: RoleSettings,
    cells: Cells | None,
    concurrency: int,
    on_done: Callable[[GroupResult], None] | None = None,
) -> GroupPass:
    """Each group read `READINGS` times, then agreed (`agree`): the first group alone (it writes the prompt cache), the
    rest in parallel. A budget stop or a stop marker ends the pass; a group not read stays a question, and its blocks
    stay uncovered."""
    out = GroupPass()
    cells = cells or {}

    def one(group: CodeGroup) -> GroupResult:
        chunk = group_chunk(group, lines)
        result = GroupResult(group, chunk)
        if out.halted:
            result.readings.append(GroupReading(out.halted, f"{NOT_READ} ({out.halted})"))
            return result
        message = f"{chunk_message(chunk, corpus, paths, cells)}\n{ASK}"
        readings: list[ChunkResult] = []
        for sample in range(READINGS):
            try:
                call = client.call(
                    role=EXTRACT,
                    step=f"kalkyl.group.{chunk.id}",
                    system=system,
                    messages=[{"role": "user", "content": message}],
                    max_tokens=settings.max_tokens,
                    effort=settings.effort,
                    sample=sample,
                )
            except (BudgetExceeded, RunStopped) as exc:
                out.halted = "budget" if isinstance(exc, BudgetExceeded) else "stopped"
                result.readings.append(GroupReading(out.halted, f"{NOT_READ} ({out.halted})"))
                break
            except ReplayMiss:
                result.readings.append(GroupReading("replay_miss", f"{NOT_READ} (saknas i stegcachen)"))
                break
            except ModelError as exc:
                result.readings.append(GroupReading(exc.kind, f"{NOT_READ} (fel: {exc.kind})"))
                break
            out.calls += 1
            out.cost_usd += call.cost_usd
            msg = call.message
            submit = next(
                (b for b in msg.content if getattr(b, "type", None) == "tool_use" and b.name == "submit_quantities"),
                None,
            )
            reading = ChunkResult(chunk.id, "submitted")
            if submit is not None and msg.stop_reason != "max_tokens":
                _review(submit.input, chunk, corpus, catalog_ids, reading, cells)
                apply_tables(reading, chunk, corpus, cells)
            readings.append(reading)
            result.readings.append(classify_reading(reading, chunk))
        if len(readings) == READINGS:
            agree(result, readings)
        if on_done:
            on_done(result)
        return result

    if not groups:
        return out
    first = one(groups[0])
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        out.groups = [first, *pool.map(one, groups[1:])]
    return out


def merge_set_asides(results: list[ChunkResult], chunks: list[Chunk], done: GroupPass) -> None:
    """The pass's agreed set-asides of marked lines, into the extraction chunk that holds them, so line coverage counts
    them as set aside with a reason."""
    owner = {b: i for i, c in enumerate(chunks) for b in c.block_ids}
    marked = {b for c in chunks for b in (*c.quantity_ids, *c.scope_ids)}
    for group in done.groups:
        for item in group.set_aside:
            by_chunk: dict[int, list[str]] = {}
            for block_id in item.block_ids:
                if block_id in marked and block_id in owner:
                    by_chunk.setdefault(owner[block_id], []).append(block_id)
            for i, ids in by_chunk.items():
                results[i].set_aside.append(item.model_copy(update={"block_ids": ids}))
