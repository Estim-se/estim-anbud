"""Kalkyl from a loaded corpus and catalog to priced posts, with no file IO: chunks -> R2 -> verify -> room
descriptors -> object model -> posts (recipe and per-space rules) -> R3 for unmapped posts -> kronor -> coverage.
`run` does the reading and writing around it."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from anbud.granskning.catalog import Catalog
from anbud.granskning.contracts import CalcPost
from anbud.granskning.corpus import Corpus
from anbud.kalkyl.coverage import uncovered_lines
from anbud.kalkyl.extract import ChunkResult, extract_all
from anbud.kalkyl.lines import Chunk, heading_paths, make_chunks
from anbud.kalkyl.match import match_posts
from anbud.kalkyl.price import (
    CommonSpaceRules,
    ObjectCount,
    Post,
    Totals,
    build_posts,
    count_objects,
    price_posts,
    totals_of,
)
from anbud.kalkyl.recipe import BoundRecipe
from anbud.kalkyl.records import Rejected, Uncovered, VerifiedRecord
from anbud.kalkyl.rooms import with_descriptor
from anbud.kalkyl.settings import EXTRACT, MATCH, KalkylConfig
from anbud.kalkyl.tables import Cells
from anbud.model_client import ModelClient


@dataclass
class KalkylResult:
    chunks: list[Chunk]
    chunk_results: list[ChunkResult]
    verified: list[VerifiedRecord]
    rejected: list[Rejected]
    objects: dict[str, ObjectCount]
    posts: list[Post]
    uncovered: list[Uncovered]
    totals: Totals
    match_stats: dict[str, Any]
    paths: dict[str, tuple[str, ...]]
    calc_posts: list[CalcPost] = field(default_factory=list)
    recipe: BoundRecipe | None = None
    rooms_from_rows: int = 0  # apartment rows whose rooms code read from the row's own descriptor

    @property
    def complete(self) -> bool:
        """Every chunk read and answered, and R3 had no failure. Otherwise the calc is marked incomplete."""
        return all(r.status == "submitted" for r in self.chunk_results) and not self.match_stats.get("error")

    def counts(self) -> dict[str, Any]:
        """Counts only: safe to print and log."""
        status: dict[str, int] = {}
        for r in self.chunk_results:
            status[r.status] = status.get(r.status, 0) + 1
        return {
            "chunks": len(self.chunks),
            "chunk_status": status,
            "quantity_lines": sum(len(c.quantity_ids) for c in self.chunks),
            "scope_lines": sum(len(c.scope_ids) for c in self.chunks),
            "records_verified": len(self.verified),
            "records_rejected": len(self.rejected),
            "tables": sum(len(r.tables) for r in self.chunk_results),
            "table_rows_read": sum(len(r.table_rows) for r in self.chunk_results),
            "table_rows_unread": sum(len(r.unread_rows) for r in self.chunk_results),
            "records_superseded_by_tables": sum(r.superseded for r in self.chunk_results),
            "object_kinds": len(self.objects),
            "rooms_from_row_descriptors": self.rooms_from_rows,
            "apartment_rows_with_rooms": sum(
                1 for v in self.verified if v.record.object_kind == "apartment" and v.record.contains
            ),
            "recipe": self.recipe.label if self.recipe else None,
            "posts": len(self.posts),
            "posts_per_object": sum(1 for p in self.posts if any(ln.per for ln in p.lines)),
            "posts_from_recipe": sum(1 for p in self.posts if any(ln.recipe for ln in p.lines)),
            "posts_assumed_per_object": sum(1 for p in self.posts if p.assumed),
            "posts_assumed_priced": self.totals.assumed_priced,
            "posts_priced": self.totals.priced,
            "posts_missing_price": self.totals.missing_price,
            "posts_missing_quantity": self.totals.missing_quantity,
            "lines_not_handled": len(self.uncovered),
            "scope_lines_not_handled": sum(u.line == "scope" for u in self.uncovered),
            "lines_set_aside_by_model": sum(u.set_aside_by_model for u in self.uncovered),
            "r3": {k: v for k, v in self.match_stats.items() if k != "error"},
            "complete": self.complete,
        }


def verified_records(chunk_results: list[ChunkResult]) -> list[VerifiedRecord]:
    out = []
    for res in chunk_results:
        for n, (record, level) in enumerate(res.records, 1):
            ids = list(dict.fromkeys(c.block_id for c in record.citations))
            out.append(
                VerifiedRecord(
                    id=f"{res.chunk_id}-r{n:03d}", chunk_id=res.chunk_id, record=record, level=level, block_ids=ids
                )
            )
    return out


def with_room_descriptors(verified: list[VerifiedRecord], corpus: Corpus) -> tuple[list[VerifiedRecord], int]:
    """Apartment list rows with the rooms their cited rows describe ("3 rok") added to `contains` where the record
    doesn't already name them, and how many records got some."""
    out, filled = [], 0
    for v in verified:
        r = v.record
        if r.record_kind == "object" and r.object_kind == "apartment" and r.quantity_source == "list_row":
            text = " | ".join(corpus.blocks[b].text for b in v.block_ids if b in corpus.blocks)
            contains = with_descriptor(list(r.contains), text)
            if len(contains) != len(r.contains):
                v = v.model_copy(update={"record": r.model_copy(update={"contains": contains})})
                filled += 1
        out.append(v)
    return out, filled


def run_kalkyl(
    corpus: Corpus,
    catalog: Catalog,
    client: ModelClient,
    kalkyl: KalkylConfig,
    on_chunk: Callable[[ChunkResult], None] | None = None,
    cells: Cells | None = None,
    recipe: BoundRecipe | None = None,
    common_spaces: CommonSpaceRules | None = None,
) -> KalkylResult:
    chunks = make_chunks(corpus, kalkyl.chunk_max_chars, kalkyl.chunk_max_quantity_lines)
    paths = heading_paths(corpus)
    results = extract_all(
        client, chunks, corpus, catalog, paths, kalkyl.roles[EXTRACT], kalkyl.concurrency, on_done=on_chunk, cells=cells
    )
    verified, filled = with_room_descriptors(verified_records(results), corpus)
    objects = count_objects(verified, recipe)
    derived = common_spaces(verified, objects) if common_spaces else []
    posts = build_posts(verified, objects, catalog, recipe, derived)
    halted = any(r.status == "not_run" for r in results)
    if halted:
        stats: dict[str, Any] = {"sent": 0, "matched": 0, "none": 0, "refused": 0, "calls": 0, "error": "halted"}
    else:
        stats = match_posts(client, posts, catalog, kalkyl.roles[MATCH], kalkyl.match_batch)
    price_posts(posts, catalog)
    return KalkylResult(
        chunks=chunks,
        chunk_results=results,
        verified=verified,
        rejected=[r for res in results for r in res.rejected],
        objects=objects,
        posts=posts,
        uncovered=uncovered_lines(chunks, results, verified),
        totals=totals_of(posts),
        match_stats=stats,
        paths=paths,
        recipe=recipe,
        rooms_from_rows=filled,
    )
