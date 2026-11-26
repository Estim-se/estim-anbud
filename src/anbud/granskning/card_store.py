"""Page cards read once per tile and kept by request content, so Granskning and Kalkyl never pay twice for a card.

The step cache can't share cards between runs: its key includes the run's salt, so a new run (Kalkyl's, or a fresh
Granskning run) misses every card an earlier run read. This store is keyed by the request alone:

    ~/estim-anbud-data/<company_id>/page-cards/<aa>/<key>.json
    key = sha256(company id + the exact page_cards request: model, effort, max_tokens, system, the tile's image)

Each file holds one tile's card as prep-like data (tile_text blocks, masked spans, markings) and the model id. A card
changes key whenever the image, the prompt, the model or the caps change, so a stale card is never reused.

`load_or_read_cards` takes each tile from, in order: this store; the step cache of earlier runs named in `from_runs`
(read directly with each run's salt, writing nothing to those runs, at $0); otherwise a paid `read_cards` call. Every
card it gets is written to the store. Granskning can switch `read_all_cards` to it without changing its outputs.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from anbud.granskning.page_cards import EFFORT, MAX_TOKENS, SYSTEM, Cards, card_from_message, messages_for, read_cards
from anbud.model_client import BudgetExceeded, ModelClient, ModelError, ReplayMiss, RunStopped
from anbud.models import Block
from anbud.prep.model import Marking, MaskedSpan, Tile
from anbud.store import RunStore, StoreError, canonical

CARD_DIR = "page-cards"
_FATAL = (BudgetExceeded, RunStopped, ReplayMiss)


def card_request(client: ModelClient, store: RunStore, tile: Tile) -> dict:
    """The exact request `read_cards` sends for this tile."""
    messages = messages_for(tile, store.get_blob(tile.blob))
    return client.request(role="page_cards", system=SYSTEM, messages=messages, max_tokens=MAX_TOKENS, effort=EFFORT)


def card_key(store: RunStore, request: dict) -> str:
    return hashlib.sha256(canonical({"company_id": store.company_id, "request": request})).hexdigest()


def _path(store: RunStore, key: str):
    return store.dir / CARD_DIR / key[:2] / f"{key}.json"


def stored_card(store: RunStore, key: str) -> Cards | None:
    path = _path(store, key)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        return Cards(
            blocks=[Block.model_validate(b) for b in raw["blocks"]],
            masked=[MaskedSpan.model_validate(m) for m in raw["masked"]],
            markings=[Marking.model_validate(m) for m in raw["markings"]],
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None  # a damaged entry is read again


def store_card(store: RunStore, key: str, tile: Tile, card: Cards, model: str) -> None:
    body = {
        "tile_id": tile.id,
        "model": model,
        "blocks": [b.model_dump(mode="json") for b in card.blocks],
        "masked": [m.model_dump(mode="json") for m in card.masked],
        "markings": [m.model_dump(mode="json") for m in card.markings],
    }
    store.write_file(_path(store, key), canonical(body))


def from_run_cache(store: RunStore, request: dict, tile: Tile, run_ids: Iterable[str]) -> Cards | None:
    """The card an earlier run read for this exact request, from its step cache. Writes nothing to that run."""
    import anthropic

    for run_id in run_ids:
        try:
            manifest = store.run_dir(run_id) / "run.json"
            if not manifest.exists():
                continue
            salt = json.loads(manifest.read_text())["salt"]
            record = store.step_get(store.step_key(salt, request, f"page_card:{tile.id}", 0))
            if record is None:
                continue
            message = anthropic.types.Message.model_validate_json(store.get_blob(record["response"]))
        except (OSError, ValueError, KeyError, StoreError):
            continue
        return card_from_message(tile, message)
    return None


@dataclass
class CardsLoaded:
    cards: Cards
    failed: dict[str, str] = field(default_factory=dict)  # tile id -> error kind
    sources: Counter = field(default_factory=Counter)  # stored, earlier_run, read


def available_cards(
    client: ModelClient, store: RunStore, tiles: Iterable[Tile], from_runs: Iterable[str] = ()
) -> tuple[Cards, Counter]:
    """The cards that cost nothing (stored, or in an earlier run's cache) and where each tile's card would come from:
    stored, earlier_run or read. No model call and no write."""
    runs = list(from_runs)
    cards, out = Cards(), Counter()
    for tile in sorted(tiles, key=lambda t: (t.doc_id, t.page, t.index, t.id)):
        request = card_request(client, store, tile)
        card = stored_card(store, card_key(store, request))
        source = "stored"
        if card is None:
            card, source = from_run_cache(store, request, tile, runs), "earlier_run"
        if card is None:
            out["read"] += 1
            continue
        out[source] += 1
        cards.blocks += card.blocks
        cards.markings += card.markings
    return cards, out


def plan_cards(client: ModelClient, store: RunStore, tiles: Iterable[Tile], from_runs: Iterable[str] = ()) -> Counter:
    """Where each tile's card would come from, without any model call or write: stored, earlier_run or read."""
    return available_cards(client, store, tiles, from_runs)[1]


def load_or_read_cards(
    client: ModelClient, store: RunStore, tiles: Iterable[Tile], from_runs: Iterable[str] = ()
) -> CardsLoaded:
    """Every tile's card, in tile order. One failed tile loses that tile; a budget stop, a stop marker or a replay miss
    ends the reading there, and the remaining tiles are listed as failed."""
    runs = list(from_runs)
    ordered = sorted(tiles, key=lambda t: (t.doc_id, t.page, t.index, t.id))
    out = CardsLoaded(Cards())
    for n, tile in enumerate(ordered):
        request = card_request(client, store, tile)
        key = card_key(store, request)
        card = stored_card(store, key)
        source = "stored"
        if card is None:
            card, source = from_run_cache(store, request, tile, runs), "earlier_run"
        if card is None:
            source = "read"
            try:
                card = read_cards(client, store, [tile])
            except _FATAL as exc:
                out.failed.update({t.id: type(exc).__name__ for t in ordered[n:]})
                break
            except (ModelError, OSError, StoreError) as exc:
                out.failed[tile.id] = exc.kind if isinstance(exc, ModelError) else type(exc).__name__
                continue
        if source != "stored":
            try:
                store_card(store, key, tile, card, request["model"])
            except (OSError, StoreError):
                pass  # the card still serves this run; the next one reads it again
        out.sources[source] += 1
        out.cards.blocks += card.blocks
        out.cards.masked += card.masked
        out.cards.markings += card.markings
        out.cards.cost_usd += card.cost_usd
    return out
