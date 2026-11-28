"""A second reading weighted by leverage: only lines whose post multiplies by an object count, or whose post is a large
share of the sum, are read again; the rest of the extraction is read once.

The second reading is a fresh sample of the chunk's first turn (`ModelClient.call(sample=1)`): the same request, so
the same context, the same prefix and the same tools. One call per chunk that holds a leverage line. A rebuild
answers it from the step cache like any other call; a run without the second reading never makes it, so a replay of
such a run stays complete.

Which lines:
- every line of a post that multiplies by an object count (`Line.per`), priced or not;
- every line of a post whose own total is at least `share` of the sum. An option counts by its own total too: read
  as included, it would move the sum by as much;
- in a chunk read again anyway, a line the first reading set aside and the second records per object: under the
  second reading its post would multiply. It costs no extra call.
Lines only the code-group pass recorded aren't read again: that pass already keeps only what two readings agree on.

For each such line, both readings' decisions are compared by kind: a record's quantity source (stated, per object,
list row, lump sum, missing), the object kind it is per and its scope; or a set-aside. Quantities are not compared,
only kinds. Where both readings decided the line and the kinds differ, the line becomes a question for the
estimator: the post keeps the first reading (so the sum is the same calc as before), carries a note, and the Frågor
sheet shows both readings with their citations. Nothing is picked silently. A line the second reading left alone is
counted as unanswered, not as a question. A budget stop or a stop marker leaves the calc as the first reading made
it but marks the run incomplete, so resuming its run id reads the rest; a model error is only counted.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from anbud.granskning.corpus import Corpus
from anbud.kalkyl.extract import ChunkResult, _review, apply_tables
from anbud.kalkyl.lines import Chunk
from anbud.kalkyl.price import OBJECT_LABELS, SCOPE_LABELS, Post
from anbud.kalkyl.records import SET_ASIDE_LABELS, QuantityRecord, VerifiedRecord
from anbud.kalkyl.settings import EXTRACT, RoleSettings
from anbud.model_client import BudgetExceeded, CallResult, ModelClient, ModelError, ReplayMiss, RunStopped

SAMPLE = 1  # the first turn's request, answered afresh
SET_ASIDE = "set_aside"
PER_OBJECT = "per_object"
SOURCE_LABELS = {
    "stated": "angiven mängd",
    PER_OBJECT: "per objekt",
    "list_row": "rad i lista",
    "lump_sum": "klumpsumma",
    "missing": "mängd saknas",
}
QUESTION_NOTE = "Fråga till kalkylatorn: två läsningar av {block} är oense ({first} / {second}), se Frågor"


@dataclass(frozen=True)
class Decision:
    """What one reading did with one line, by kind."""

    kinds: tuple[str, ...]  # sorted; ("set_aside",) for a set-aside
    label: str  # Swedish, for the calc
    quotes: tuple[str, ...] = ()


@dataclass
class Question:
    block_id: str
    post_ids: list[str]  # empty for a line the first reading set aside
    first: Decision
    second: Decision


@dataclass
class SecondReading:
    lines: list[str] = field(default_factory=list)  # the lines compared, in reading order
    chunks: list[str] = field(default_factory=list)  # the chunks read again
    calls: int = 0
    cost_usd: float = 0.0
    agreed: int = 0
    unanswered: int = 0
    questions: list[Question] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)  # chunk id -> error kind
    halted: str | None = None  # "budget" or "stopped": the run is incomplete, and resuming it reads the rest

    def summary(self) -> dict[str, Any]:
        """Counts only: safe to print and to log."""
        return {
            "lines": len(self.lines),
            "chunks": len(self.chunks),
            "calls": self.calls,
            "cost_usd": round(self.cost_usd, 4),
            "agreed": self.agreed,
            "questions": len(self.questions),
            "unanswered": self.unanswered,
            "errors": len(self.errors),
            "halted": self.halted,
        }


def leverage(posts: list[Post], total: Decimal, share: float) -> dict[str, list[str]]:
    """Leverage lines -> the posts they feed: every line of a post that multiplies by an object count, or whose total
    is at least `share` of the sum."""
    out: dict[str, list[str]] = {}
    for post in posts:
        multiplies = any(ln.per for ln in post.lines)
        large = post.total is not None and total > 0 and post.total >= Decimal(str(share)) * total
        if not (multiplies or large):
            continue
        for ln in post.lines:
            for block_id in ln.record.block_ids:
                if post.id not in out.setdefault(block_id, []):
                    out[block_id].append(post.id)
    return out


def _kind(record: QuantityRecord) -> str:
    return ":".join([record.quantity_source, *([record.per_object] if record.per_object else []), record.scope])


def _label(record: QuantityRecord) -> str:
    parts = [SOURCE_LABELS.get(record.quantity_source, record.quantity_source)]
    if record.per_object:
        parts[0] += f" ({OBJECT_LABELS.get(record.per_object, (record.per_object,))[0]})"
    if record.quantity is not None:
        parts.append(f"{record.quantity:g} {record.unit}")
    parts.append(SCOPE_LABELS.get(record.scope, record.scope))
    return ", ".join(parts)


def decisions(
    records: list[tuple[QuantityRecord, list[str]]], set_aside: list[Any], lines: set[str]
) -> dict[str, Decision]:
    """Each line's decision in one reading: its records' kinds, else its set-aside. Lines it left alone are absent."""
    out: dict[str, Decision] = {}
    for item in set_aside:
        for block_id in item.block_ids:
            if block_id in lines and item.kind != "unclear":
                label = f"bortvald: {SET_ASIDE_LABELS.get(item.kind, item.kind)} ({item.reason})"
                out[block_id] = Decision((SET_ASIDE,), label)
    found: dict[str, list[QuantityRecord]] = {}
    for record, block_ids in records:
        for block_id in block_ids:
            if block_id in lines:
                found.setdefault(block_id, []).append(record)
    for block_id, recs in found.items():
        out[block_id] = Decision(
            tuple(sorted({_kind(r) for r in recs})),
            "; ".join(dict.fromkeys(_label(r) for r in recs)),
            tuple(c.quote for r in recs for c in r.citations if c.block_id == block_id),
        )
    return out


def first_decisions(verified: list[VerifiedRecord], results: list[ChunkResult], lines: set[str]) -> dict[str, Decision]:
    return decisions([(v.record, v.block_ids) for v in verified], [s for r in results for s in r.set_aside], lines)


def second_decisions(readings: list[ChunkResult], lines: set[str]) -> dict[str, Decision]:
    records = [(r, [c.block_id for c in r.citations]) for reading in readings for r, _ in reading.records]
    return decisions(records, [s for reading in readings for s in reading.set_aside], lines)


def multiplied_set_asides(first: dict[str, Decision], second: dict[str, Decision]) -> list[str]:
    """Lines the first reading set aside and the second records per object."""
    return [
        b
        for b, d in second.items()
        if first.get(b, Decision((), "")).kinds == (SET_ASIDE,) and any(k.startswith(PER_OBJECT) for k in d.kinds)
    ]


def compare(
    first: dict[str, Decision], second: dict[str, Decision], lines: dict[str, list[str]], out: SecondReading
) -> None:
    for block_id in out.lines:
        if block_id not in second:
            out.unanswered += 1
        elif block_id in first and first[block_id].kinds != second[block_id].kinds:
            out.questions.append(Question(block_id, lines.get(block_id, []), first[block_id], second[block_id]))
        else:
            out.agreed += 1


def read_again(
    client: ModelClient,
    chunks: list[Chunk],
    results: list[ChunkResult],
    verified: list[VerifiedRecord],
    posts: list[Post],
    total: Decimal,
    share: float,
    corpus: Corpus,
    catalog_ids: set[str],
    system: list[dict[str, Any]],
    message_of: Callable[[Chunk], str],
    settings: RoleSettings,
    cells: Any,
    concurrency: int,
) -> SecondReading:
    """The second reading of the leverage lines, compared with the first. Notes the posts a question touches."""
    lines = leverage(posts, total, share)
    chunk_of: dict[str, str] = {}  # a line's chunk: the chunk of the first record that cites it
    for v in verified:
        for block_id in v.block_ids:
            chunk_of.setdefault(block_id, v.chunk_id)
    wanted = {chunk_of[b] for b in lines if b in chunk_of}
    todo = [c for c in chunks if c.id in wanted]  # extraction chunks only: a group-pass chunk is never in `chunks`
    out = SecondReading(chunks=[c.id for c in todo])
    halted: list[str] = []

    def one(chunk: Chunk) -> tuple[ChunkResult | None, str | None, CallResult | None]:
        """A chunk's second reading, or the error kind that stopped it, and the call when one came back."""
        if halted:
            return None, halted[0], None
        try:
            call = client.call(
                role=EXTRACT,
                step=f"kalkyl.extract.{chunk.id}",
                system=system,
                messages=[{"role": "user", "content": message_of(chunk)}],
                max_tokens=settings.max_tokens,
                effort=settings.effort,
                sample=SAMPLE,
            )
        except (BudgetExceeded, RunStopped) as exc:
            halted.append("budget" if isinstance(exc, BudgetExceeded) else "stopped")
            return None, halted[-1], None
        except ReplayMiss:
            return None, "replay_miss", None
        except ModelError as exc:
            return None, exc.kind, None
        msg = call.message
        submit = next(
            (b for b in msg.content if getattr(b, "type", None) == "tool_use" and b.name == "submit_quantities"), None
        )
        if submit is None or msg.stop_reason == "max_tokens":
            return None, "truncated" if msg.stop_reason == "max_tokens" else "no_submit", call
        reading = ChunkResult(chunk.id, "submitted")
        _review(submit.input, chunk, corpus, catalog_ids, reading, cells or {})
        apply_tables(reading, chunk, corpus, cells or {})
        return reading, None, call

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        answers = list(pool.map(one, todo))
    readings: list[ChunkResult] = []
    for chunk, (reading, error, call) in zip(todo, answers, strict=True):
        if call is not None:
            out.calls += 1
            out.cost_usd += call.cost_usd
        if reading is None:
            out.errors[chunk.id] = error or "no_submit"
        else:
            readings.append(reading)
    out.halted = halted[0] if halted else None
    done = {r.chunk_id for r in readings}
    read = {b for c in todo if c.id in done for b in c.block_ids}  # every line of a chunk read again
    first = first_decisions(verified, results, read)
    second = second_decisions(readings, read)
    asked = [b for b in lines if chunk_of.get(b) in done] + multiplied_set_asides(first, second)
    order = {b: i for i, b in enumerate(corpus.order)}
    out.lines = sorted(dict.fromkeys(asked), key=lambda b: order.get(b, len(order)))
    compare(first, second, lines, out)
    by_id = {p.id: p for p in posts}
    for q in out.questions:
        for post_id in q.post_ids:
            by_id[post_id].notes.append(
                QUESTION_NOTE.format(block=q.block_id, first=q.first.label, second=q.second.label)
            )
    return out
