"""Rank, cap and coverage (plan §6.1 step 13), all in code.

- Score: kronor high (an unpriced flag counts as `unpriced_sek`) x verdict weight x the lens action rate, times the
  covered-by-calc and other-trade adjustments when the verifier says so.
- At most `max_shown` priced flags are shown; generic-lens flags are a separate list of at most `max_generic`.
- Flags kronor can't price, and that the verifier didn't refute, are a list of their own ("Krav utan prisuppgift", at
  most `max_unpriced_shown`), so a flat unpriced weight never lets priced flags push them out. They are ordered by the
  verifier's outcome (confirmed, partly, unverified), then by severity: contractual exposure, the electrical scope,
  how many lenses agree, and the lenses' action rate. No kronor weight enters that order.
- Everything else is kept, with where it went and why, for the log and the XLSX.
- Coverage per lens comes only from the lens's tool log, with the corpus used to map ids to documents. It lists what
  was read and what wasn't, and never says "all clear".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from anbud.granskning.catalog import Catalog
from anbud.granskning.contracts import CalcPost, LensResult
from anbud.granskning.corpus import Corpus
from anbud.granskning.items import Item
from anbud.granskning.kronor import is_minor, price, ranking_kronor
from anbud.granskning.settings import Settings

NOT_ALL_CLEAR = (
    "Täckningen visar bara vad granskningen läste och sökte. Den betyder inte att underlaget saknar fel eller risker."
)


def price_items(
    items: list[Item], catalog: Catalog | None, calc_posts: dict[str, CalcPost], settings: Settings
) -> list[Item]:
    """Kronor for each item: from the verifier's corrected money basis when it gave one and didn't refute the flag,
    otherwise from the flag's own, or, when that can't be priced, from the first merged lens flag whose basis can."""
    out = []
    for item in items:
        if item.verdict and item.verdict.verdict != "refuted" and item.verdict.corrected_money_basis:
            bases = [item.verdict.corrected_money_basis]
        else:
            bases = [item.flag.money_basis]
            bases += [f.money_basis for f in item.source_flags if f.money_basis not in bases]
        priced = [price(basis, catalog, calc_posts, settings) for basis in bases]
        kronor = next((k for k in priced if k.priced), priced[0])
        out.append(item.model_copy(update={"kronor": kronor, "minor": is_minor(kronor, item.flag, settings)}))
    return out


def action_rate(item: Item, settings: Settings, action_rates: dict[str, float] | None = None) -> float:
    rates = action_rates or {}
    return max((rates.get(lens, settings.default_action_rate) for lens in item.lenses), default=1.0)


def factor(item: Item, settings: Settings, action_rates: dict[str, float] | None = None) -> float:
    """Everything in the score but the kronor: verdict weight x lens action rate x the verifier's adjustments."""
    value = settings.weight(item.verdict.verdict if item.verdict else None) * action_rate(item, settings, action_rates)
    if item.verdict and item.verdict.covered_by_calc:
        value *= settings.covered_by_calc
    if item.verdict and not item.verdict.electrical_scope:
        value *= settings.other_trade
    return value


def score(item: Item, settings: Settings, action_rates: dict[str, float] | None = None) -> float:
    return ranking_kronor(item.kronor, settings) * factor(item, settings, action_rates)


@dataclass
class Ranked:
    shown: list[Item] = field(default_factory=list)
    generic: list[Item] = field(default_factory=list)
    logged: list[Item] = field(default_factory=list)
    unpriced: list[Item] = field(default_factory=list)


_OUTCOME_ORDER = {"confirmed": 0, "partly": 1}  # anything else (no verdict) is unverified, after both


def is_unpriced(item: Item) -> bool:
    return item.kronor is None or not item.kronor.priced


def unpriced_order(item: Item, rate: float = 1.0) -> tuple[int, bool, bool, int, float, str]:
    """Verifier outcome, then severity. Flags carry no severity field, so severity is what the flag and the verifier
    say: contractual exposure first, then in the electrical scope, then more lenses agreeing, then the lens's rate."""
    outcome = _OUTCOME_ORDER.get(item.verdict.verdict if item.verdict else "", 2)
    other_trade = bool(item.verdict and not item.verdict.electrical_scope)
    return outcome, not item.flag.contractual_exposure, other_trade, -len(item.lenses), -rate, item.fingerprint


def unpriced_queue(items: list[Item], settings: Settings, action_rates: dict[str, float] | None = None) -> list[Item]:
    """The flags for "Krav utan prisuppgift", in its order. Before verification every outcome is unverified, so the
    order is severity alone; the verifier's reserved slots are drawn from the front of this queue."""
    queue = [i for i in items if _unpriced_candidate(i, factor(i, settings, action_rates))]
    return sorted(queue, key=lambda i: unpriced_order(i, action_rate(i, settings, action_rates)))


def rank(items: list[Item], settings: Settings, action_rates: dict[str, float] | None = None) -> Ranked:
    scored = [i.model_copy(update={"score": score(i, settings, action_rates)}) for i in items]
    scored.sort(key=lambda i: (-i.score, i.fingerprint))
    ranked = Ranked()
    unpriced = unpriced_queue(scored, settings, action_rates)
    candidate = {i.fingerprint for i in unpriced}
    for n, item in enumerate(unpriced):
        placement = "shown_unpriced" if n < settings.max_unpriced_shown else "capped"
        placed = item.model_copy(update={"placement": placement})
        (ranked.unpriced if placement == "shown_unpriced" else ranked.logged).append(placed)
    for item in scored:
        if item.fingerprint in candidate:
            continue
        if item.verdict and item.verdict.verdict == "refuted":
            placement = "refuted"
        elif item.verdict and item.verdict.covered_by_calc and item.score <= 0:
            placement = "covered"
        elif item.score <= 0:
            placement = "zero"
        elif item.minor:
            placement = "minor"
        elif item.generic:
            placement = "generic" if len(ranked.generic) < settings.max_generic else "capped"
        else:
            placement = "shown" if len(ranked.shown) < settings.max_shown else "capped"
        placed = item.model_copy(update={"placement": placement})
        {"shown": ranked.shown, "generic": ranked.generic}.get(placement, ranked.logged).append(placed)
    return ranked


def _unpriced_candidate(item: Item, weight: float) -> bool:
    """Unpriced, not refuted, not weighted to zero (covered by the calc), and not a generic-lens flag (those have
    their own list). The kronor stand-in plays no part."""
    if not is_unpriced(item) or item.generic or weight <= 0:
        return False
    return not (item.verdict and item.verdict.verdict == "refuted")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LensCoverage(_Model):
    lens_id: str
    status: str
    generic: bool = False
    turns: int = 0
    cost_usd: float = 0.0
    documents_opened: list[str] = Field(default_factory=list, description="Document names, from read blocks and tiles")
    blocks_read: int = 0
    searches: list[str] = Field(default_factory=list)
    sheets_opened: list[str] = Field(default_factory=list)
    tiles_viewed: int = 0
    calc_calls: int = 0
    law_lookups: int = 0
    catalog_searches: int = 0
    refused_calls: int = 0
    failed_calls: int = 0
    not_checked: list[str] = Field(default_factory=list)


class TenderCoverage(_Model):
    documents: int
    not_read_by_prep: list[str] = Field(default_factory=list)
    named_not_present: list[str] = Field(default_factory=list)
    unread_tiles: list[str] = Field(default_factory=list, description="Drawing or scan tiles no page card was read for")
    lenses: list[LensCoverage] = Field(default_factory=list)
    note: str = NOT_ALL_CLEAR


def document_names(corpus: Corpus) -> dict[str, str]:
    """doc_id -> the file's name (no folders), or the doc id when prep recorded no path."""
    names = {}
    for doc in corpus.documents:
        paths = doc.get("paths") or []
        names[doc["doc_id"]] = PurePath(paths[0]).name if paths else doc["doc_id"]
    for block in corpus.blocks.values():
        names.setdefault(block.doc_id, block.doc_id)
    return names


def _visual_docs(corpus: Corpus) -> set[str]:
    docs = {t.doc_id for t in corpus.tiles.values()}
    for doc in corpus.documents:
        if any(p.get("kind") in ("scan", "drawing") for p in doc.get("pages") or []):
            docs.add(doc["doc_id"])
    return docs


_STATUS_NOTES = {
    "truncated": "Linsen nådde sin gräns för varv eller kostnad innan den lämnade sina fynd.",
    "no_submit": "Linsen lämnade aldrig in några fynd.",
    "error": "Linsen avbröts av ett fel.",
}


def lens_coverage(result: LensResult, corpus: Corpus, names: dict[str, str] | None = None) -> LensCoverage:
    names = names or document_names(corpus)
    opened: dict[str, None] = {}
    sheets: dict[str, None] = {}
    blocks: set[str] = set()
    tiles: set[str] = set()
    searches: list[str] = []
    counts = {"calc_posts": 0, "law_lookup": 0, "catalog_search": 0, "refused": 0, "failed": 0}
    for call in result.tool_log:
        if call.refused is not None:
            counts["refused"] += 1
            continue
        if call.tool == "law_lookup":  # counted as a lookup even when the harness has no law corpus yet
            counts["law_lookup"] += 1
            if call.error is not None and call.error != "not_available":
                counts["failed"] += 1
            continue
        if call.error is not None:
            counts["failed"] += 1
            continue
        if call.tool == "search_blocks":
            searches.append(str(call.input.get("query", "")))
        elif call.tool == "read_blocks":
            for block_id in call.result_ids:
                block = corpus.get(block_id)
                if block is None:
                    continue
                blocks.add(block_id)
                opened[block.doc_id] = None
                if block.sheet is not None:
                    sheets[f"{names.get(block.doc_id, block.doc_id)} / {block.sheet}"] = None
        elif call.tool == "view_tile":
            for tile_id in call.result_ids or [str(call.input.get("tile", ""))]:
                tile = corpus.tiles.get(tile_id)
                if tile is not None:
                    tiles.add(tile_id)
                    opened[tile.doc_id] = None
        elif call.tool in counts:
            counts[call.tool] += 1
    not_checked = []
    if result.status in _STATUS_NOTES:
        not_checked.append(_STATUS_NOTES[result.status])
    all_docs = list(names)
    unopened = [names[d] for d in all_docs if d not in opened]
    if unopened:
        not_checked.append(f"{len(unopened)} av {len(all_docs)} dokument öppnades inte: " + ", ".join(unopened))
    tile_docs = {t.doc_id for t in corpus.tiles.values() if t.id in tiles}
    unseen = [names.get(d, d) for d in sorted(_visual_docs(corpus)) if d not in tile_docs]
    if unseen:
        not_checked.append("Ritningar eller skannade sidor som linsen inte tittade på: " + ", ".join(unseen))
    if not searches:
        not_checked.append("Linsen sökte inte i underlaget.")
    return LensCoverage(
        lens_id=result.lens_id,
        status=result.status,
        generic=result.generic,
        turns=result.turns,
        cost_usd=result.cost_usd,
        documents_opened=[names[d] for d in opened if d in names],
        blocks_read=len(blocks),
        searches=searches,
        sheets_opened=list(sheets),
        tiles_viewed=len(tiles),
        calc_calls=counts["calc_posts"],
        law_lookups=counts["law_lookup"],
        catalog_searches=counts["catalog_search"],
        refused_calls=counts["refused"],
        failed_calls=counts["failed"],
        not_checked=not_checked,
    )


def coverage(
    results: list[LensResult],
    corpus: Corpus,
    named_not_present: list[str] | None = None,
    unread_tiles: list[str] | None = None,
) -> TenderCoverage:
    names = document_names(corpus)
    skipped = [f"{names.get(d['doc_id'], d['doc_id'])} ({d['skipped']})" for d in corpus.documents if d.get("skipped")]
    return TenderCoverage(
        documents=len(names),
        not_read_by_prep=skipped,
        named_not_present=list(named_not_present or []),
        unread_tiles=list(unread_tiles or []),
        lenses=[lens_coverage(r, corpus, names) for r in results],
    )


def summary(ranked: Ranked) -> dict[str, Any]:
    """Counts for the run log: ids and numbers only."""
    placements: dict[str, int] = {}
    for item in ranked.shown + ranked.generic + ranked.logged:
        placements[item.placement or "none"] = placements.get(item.placement or "none", 0) + 1
    return {"placements": placements, "shown": [i.fingerprint for i in ranked.shown]}
