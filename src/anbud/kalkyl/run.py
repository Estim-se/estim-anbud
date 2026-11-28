"""`anbud kalkyl`: a dev tender's prepared blocks to a priced calc in the tender's data folder.

Reads `<tender>/prep/`, the page cards of its tiles (`granskning.card_store`: stored cards, then earlier Granskning
runs' step caches at $0, then paid R1 reads), the catalog from `~/estim-anbud-data/catalogs/<name>.json` and the
recipe (`kalkyl.recipe`: the customer's, else Estim's default).
Writes, under `<tender>/kalkyl/<run_id>/`:

    kalkyl.xlsx    the calc, Swedish labels, catalog label on every sheet
    posts.json     list[CalcPost], the shared contract
    result.json    posts, records, object model and uncovered lines with their citations (customer data, stays here)
    summary.json   counts and cost only

`plan()` is the $0 dry run: chunk counts and a cost estimate, with no model call.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from anbud import paths
from anbud.config import Config, load_config
from anbud.frozen import freeze_tree, is_frozen
from anbud.granskning.card_store import CardsLoaded, available_cards, load_or_read_cards
from anbud.granskning.catalog import Catalog
from anbud.granskning.corpus import Corpus
from anbud.granskning.pipeline import add_cards
from anbud.kalkyl.catalog import CatalogError, load, prompt_lines
from anbud.kalkyl.code_groups import for_result as code_coverage
from anbud.kalkyl.extract import BRIEF, chunk_message, outline_text
from anbud.kalkyl.group_pass import READINGS
from anbud.kalkyl.group_pass import bound as group_bound
from anbud.kalkyl.lines import heading_paths, make_chunks
from anbud.kalkyl.pipeline import KalkylResult, run_kalkyl
from anbud.kalkyl.price import calc_posts
from anbud.kalkyl.recipe import BoundRecipe, RecipeError, bind, load_recipe
from anbud.kalkyl.repetition import RepetitionError, load_repetition
from anbud.kalkyl.repetition import customer_path as repetition_path
from anbud.kalkyl.settings import EXTRACT, MATCH, KalkylConfig, load_kalkyl_config
from anbud.kalkyl.tables import load_cells
from anbud.kalkyl.xlsx import write_workbook
from anbud.model_client import Budget, ModelClient
from anbud.prep.tender import CELLS_FILE, IntakeStop
from anbud.selection import OVERRIDE_FILE, SELECTION_FILE, Selection, SelectionError, load_selected
from anbud.stops import estimate_label, kalkyl_stop
from anbud.store import RunStore, canonical

READ_STEP = "kalkyl.extract."  # every R2 reading call's step starts so (`extract.extract_chunk`)
CHARS_PER_TOKEN = 3.0  # Swedish text with block markup; conservative, so the estimate errs high
OUTPUT_TOKENS_PER_CHUNK = (3000, 8000)  # records plus adaptive thinking at medium effort, low and high
CARD_USD_PER_TILE = 0.03  # an R1 card that has to be read (plan section 9.1: 10 tiles about $0.17), rounded up


class KalkylError(RuntimeError):
    """Raised with a reason that names no tender content."""


def load_corpus(prep_dir: Path) -> tuple[Corpus, Selection]:
    """The prepared corpus with the selection applied: other trades' documents outside what the models read."""
    if not (prep_dir / "blocks.jsonl").exists():
        raise KalkylError("the tender has no prep output; run `anbud prep` first")
    try:
        return load_selected(prep_dir)
    except SelectionError as exc:
        raise KalkylError(str(exc)) from None


def card_source_runs(store: RunStore, tender_id: str) -> list[str]:
    """Earlier Granskning runs of this tender (`anbud run` names them `<tender_id>-<stamp>`), newest first: their step
    caches hold page cards Kalkyl can reuse at $0."""
    runs = store.dir / "runs"
    if not runs.is_dir():
        return []
    found = [p.name for p in runs.glob(f"{tender_id}-*") if (p / "run.json").exists()]
    return sorted(found, reverse=True)


def with_cards(client: ModelClient, store: RunStore, corpus: Corpus, tender_id: str) -> tuple[Corpus, CardsLoaded]:
    loaded = load_or_read_cards(client, store, corpus.tiles.values(), card_source_runs(store, tender_id))
    if loaded.cards.markings:
        raise IntakeStop(loaded.cards.markings)
    return add_cards(corpus, loaded.cards), loaded


def _decimal(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(type(value).__name__)


def result_json(result: KalkylResult) -> bytes:
    posts = []
    for p in result.posts:
        posts.append(
            {
                "id": p.id,
                "name": p.name,
                "scope": p.scope,
                "unit": p.unit,
                "type_code": p.type_code,
                "catalog_id": p.catalog_id,
                "mapped_by": p.mapped_by,
                "mapping_note": p.mapping_note,
                "quantity": p.quantity,
                "basis": p.basis,
                "notes": p.notes,
                "status": p.status,
                "level": p.level,
                "unit_price": p.unit_price,
                "hours_per_unit": p.hours_per_unit,
                "material": p.material,
                "hours": p.hours,
                "labour": p.labour,
                "total": p.total,
                "records": [ln.record.id for ln in p.lines],
                "per_object": sorted({ln.per for ln in p.lines if ln.per}),
                "recipe": next((ln.recipe for ln in p.lines if ln.recipe), None),
                "assumed_per_object": p.assumed_per_object,
                "assumed_object_count": p.assumed_objects,
                "hours_flat": p.hours_flat,
                "repetition_factor": p.repetition_factor,
                "installed_unit_price": p.installed_unit_price,
                "object_rows": p.rows,
            }
        )
    body = {
        "complete": result.complete,
        "totals": vars(result.totals),
        "objects": [vars(o) for o in result.objects.values()],
        "recipe": {"label": result.recipe.label, "source": result.recipe.source} if result.recipe else None,
        "posts": posts,
        "records": [v.model_dump(mode="json") for v in result.verified],
        "connections": [v.model_dump(mode="json") for v in result.connections],
        "rejected": [r.model_dump(mode="json") for r in result.rejected],
        "uncovered": [u.model_dump(mode="json") for u in result.uncovered],
        "chunks": [
            {"id": r.chunk_id, "status": r.status, "turns": r.turns, "cost_usd": r.cost_usd, "error": r.error}
            for r in result.chunk_results
        ],
    }
    if result.group_pass is not None:  # only when it ran: a run without it writes the same bytes as before
        body["group_pass"] = {
            "summary": result.group_pass.summary(result.posts),
            "groups": [
                {
                    "code": g.group.code,
                    "chunk": g.chunk.id,
                    "blocks": len(g.chunk.block_ids),
                    "outcome": g.outcome,
                    "readings": [r.outcome for r in g.readings],
                    "records": len(g.records),
                    "set_aside": sum(len(s.block_ids) for s in g.set_aside),
                }
                for g in result.group_pass.groups
            ],
        }
    return json.dumps(body, ensure_ascii=False, indent=1, default=_decimal).encode()


def load_bound_recipe(company_id: str, catalog: Catalog, root: Path | None = None) -> BoundRecipe | None:
    """The customer's recipe, else Estim's default, matched to the catalog; None when neither exists."""
    try:
        return bind(load_recipe(company_id, root), catalog)
    except RecipeError as exc:
        raise KalkylError(str(exc)) from None


def _file_sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def input_hashes(company_id: str, tender: Path, catalog_name: str, root: Path | None = None) -> dict[str, str | None]:
    """sha256 of every input file the run reads, by role. File names only, never a path or a tender name."""
    from anbud.kalkyl.recipe import customer_path, default_path

    base = (root or paths.data_root()).expanduser()
    return {
        "blocks": _file_sha(tender / "prep" / "blocks.jsonl"),
        "cells": _file_sha(tender / "prep" / CELLS_FILE),
        "tiles": _file_sha(tender / "prep" / "tiles.json"),
        "catalog": _file_sha(base / "catalogs" / f"{catalog_name}.json"),
        "customer_recipe": _file_sha(customer_path(company_id, root)),
        "default_recipe": _file_sha(default_path(root)),
        "customer_repetition": _file_sha(repetition_path(company_id, root)),
        "selection_override": _file_sha(tender / OVERRIDE_FILE),
    }


def new_run_id(tender_id: str) -> str:
    return f"kalkyl-{tender_id}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def base_salt(store: RunStore, run_id: str) -> str:
    """The salt of a stored run, read without writing anything to it (a frozen run is read-only)."""
    salt = store.stored_salt(run_id)
    if salt is None:
        raise KalkylError(f"no stored run {run_id} to rebuild from")
    return salt


def run_dev_tender(
    company_id: str,
    tender_id: str,
    *,
    sdk: Any,
    catalog_name: str | None = None,
    run_id: str | None = None,
    root: Path | None = None,
    config: Config | None = None,
    kalkyl: KalkylConfig | None = None,
    code: dict[str, Any] | None = None,
    base_run: str | None = None,
) -> dict[str, Any]:
    """Run Kalkyl on a dev tender (the caller has checked it is one) and write the outputs. Returns counts only.
    With `sdk=None` the run is a replay: every answer must come from the run's step cache or the card store.

    `code` is the provenance block (harness commit, dirty flag, config hashes); the summary also records sha256
    hashes of the inputs read (blocks, cells, catalog, recipe), so a frozen run can be traced to what it ran on.

    `base_run` rebuilds from a stored run: a new run with that run's salt, so every request identical to one the base
    run made (its extraction, unless the prompt or the chunks changed) is answered from the step cache at $0, and a
    changed request is sent (or, in a replay, counted as a miss). Nothing is written to the base run.

    A run that finished (every chunk read, no replay miss, no failed store write, no unread tile) is frozen at the end:
    its outputs folder and run record become read-only. A frozen run is never resumed or replayed in place; a rebuild
    from it gets its own run id. An unfinished run stays writable, so the same run id resumes it."""
    config = config or load_config()
    kalkyl = kalkyl or load_kalkyl_config()
    tender = paths.dev_tender_dir(company_id, tender_id, root)
    corpus, selection = load_corpus(tender / "prep")
    catalog = load(catalog_name or kalkyl.default_catalog, root)
    recipe = load_bound_recipe(company_id, catalog, root)
    store = RunStore(company_id, root)
    run_id = run_id or new_run_id(tender_id)
    if base_run is not None and run_id == base_run:
        raise KalkylError("a rebuild needs its own run id")
    out = tender / "kalkyl" / run_id
    if is_frozen(out) or is_frozen(store.run_dir(run_id)):
        hint = "start a new --run-id (--base-run reuses its extraction)" if sdk else "replay it with --base-run"
        raise KalkylError(f"run {run_id} is finished and frozen; {hint}")
    salt = base_salt(store, base_run) if base_run is not None else None
    stored_base = store.stored_base_run(run_id)
    if base_run is not None and stored_base is not None and stored_base != base_run:
        raise KalkylError(f"run {run_id} is a rebuild from {stored_base}; resume it with that base or without one")
    store.open_run(run_id, salt=salt, code=code, base_run=base_run)
    base_run = base_run or stored_base  # a resumed rebuild keeps the base it started from
    cells = load_cells(tender / "prep" / CELLS_FILE)
    estimate = _estimate(config, kalkyl, corpus, cells, catalog, store, company_id, tender_id)
    stop = kalkyl_stop(kalkyl.stops, estimate["cost_usd_est_high"], kalkyl.margin)  # before any paid call
    budget = Budget(stop.usd)
    client = ModelClient(config, sdk, budget, run_id=run_id, company_id=company_id, store=store, salt=salt)
    corpus, cards = with_cards(client, store, corpus, tender_id)
    try:
        repetition = load_repetition(company_id, root)
    except RepetitionError as exc:
        raise KalkylError(str(exc)) from None
    result = run_kalkyl(corpus, catalog, client, kalkyl, cells=cells, recipe=recipe, repetition=repetition)
    meta = {
        "company_id": company_id,
        "tender_id": tender_id,
        "run_id": run_id,
        "cost_usd": budget.spent,
        "stop": estimate_label(stop),
    }
    workbook, rows = write_workbook(result, corpus, catalog, meta)
    result.calc_posts = calc_posts(result.posts, rows)
    store.write_file(out / "kalkyl.xlsx", workbook)
    store.write_file(out / "posts.json", canonical([p.model_dump(mode="json") for p in result.calc_posts]))
    store.write_file(out / "result.json", result_json(result))
    store.write_file(out / SELECTION_FILE, canonical(selection.to_json()))
    if cards.cards.masked:  # the originals stay local, like prep's masking report
        store.write_file(out / "cards_masking.json", canonical([m.model_dump(mode="json") for m in cards.cards.masked]))
    summary = {
        "run_id": run_id,
        "base_run": base_run,
        "code": code,
        "inputs": input_hashes(company_id, tender, catalog_name or kalkyl.default_catalog, root),
        "catalog": catalog.name,
        "tiles": len(corpus.tiles),
        "cards": dict(cards.sources),
        "tiles_unread": len(cards.failed),
        "repetition": repetition.label if repetition else None,
        "selection": selection.summary(),
        "code_groups": code_coverage(result, corpus).summary(),
        **({"group_pass": result.group_pass.summary(result.posts)} if result.group_pass is not None else {}),
        **result.counts(),
        "cost_usd": round(budget.spent, 4),
        "stop": {**stop.summary(), "cost_usd_est_high": estimate["cost_usd_est_high"], "margin": kalkyl.margin},
        "persist_failures": client.persist_failures,
        "replay_misses": client.replay_misses,
        "halted": client.halted,  # calls refused by a limit: the hard stop, the spend cap or a stop marker
        "failures": dict(sorted(client.failures.items())),  # calls that failed for good, by kind
        "unanswered": client.unanswered(READ_STEP),  # calls failed and no reading call was answered
        "output": str(out),
    }
    finished = result.complete and not client.replay_misses and not client.persist_failures and not cards.failed
    summary["frozen"] = finished
    store.write_file(out / "summary.json", json.dumps(summary, indent=2, sort_keys=True).encode())
    if finished:  # returned, not written: summary.json is read-only from here
        summary["freeze_failures"] = len(freeze_tree(out) + freeze_tree(store.run_dir(run_id)))
    return summary


def plan(
    company_id: str,
    tender_id: str,
    *,
    catalog_name: str | None = None,
    root: Path | None = None,
    kalkyl: KalkylConfig | None = None,
) -> dict:
    """The $0 dry run: what a run would send, in counts, a cost range from config prices and the hard stop's tier the
    run will pick from it. No model is called, no key is read and nothing is written. Cards already stored or in an
    earlier run's cache are counted in, and their blocks are chunked as the run will chunk them."""
    config = load_config()
    kalkyl = kalkyl or load_kalkyl_config()
    prep_dir = paths.dev_tender_dir(company_id, tender_id, root) / "prep"
    corpus, selection = load_corpus(prep_dir)
    cells = load_cells(prep_dir / CELLS_FILE)
    repetition = load_repetition(company_id, root)
    try:
        catalog: Catalog | None = load(catalog_name or kalkyl.default_catalog, root)
    except CatalogError:
        catalog = None
    recipe = load_bound_recipe(company_id, catalog, root) if catalog else None
    estimate = _estimate(config, kalkyl, corpus, cells, catalog, RunStore(company_id, root), company_id, tender_id)
    stop = kalkyl_stop(kalkyl.stops, estimate["cost_usd_est_high"], kalkyl.margin)
    return {
        "chunks": estimate["chunks"],
        "quantity_lines": estimate["quantity_lines"],
        "scope_lines": estimate["scope_lines"],
        "rows_with_cells": len(cells),
        "selection": selection.summary(),
        "recipe": recipe.label if recipe else None,
        "repetition": repetition.label if repetition else None,
        "recipe_items": len(recipe.counts) if recipe else 0,
        "recipe_items_unbound": recipe.unbound if recipe else 0,
        **{k: v for k, v in estimate.items() if k not in ("chunks", "quantity_lines", "scope_lines")},
        "catalog": catalog.name if catalog else "missing",
        "catalog_items": len(catalog.items) if catalog else 0,
        "hard_stop_usd": stop.usd,
        "stop_tier": stop.tier,
        "stop_picked_from_usd": stop.size,  # cost_usd_est_high times the margin
        "stop_above_top_tier": stop.above_top,
        "max_tokens_per_r2_call": kalkyl.roles[EXTRACT].max_tokens,
    }


def _estimate(
    config: Config,
    kalkyl: KalkylConfig,
    corpus: Corpus,
    cells: Any,
    catalog: Catalog | None,
    store: RunStore,
    company_id: str,
    tender_id: str,
) -> dict[str, Any]:
    """Chunk counts and the cost range, at $0: the dry run's numbers, and what a run picks its hard stop's tier from
    before its first paid call."""
    offline = ModelClient(config, None, Budget(0.0), run_id="plan", company_id=company_id)  # no store: writes nothing
    free, sources = available_cards(offline, store, corpus.tiles.values(), card_source_runs(store, tender_id))
    corpus = add_cards(corpus, free)  # chunks as the run will cut them, as far as the cards are known
    chunks = make_chunks(corpus, kalkyl.chunk_max_chars, kalkyl.chunk_max_quantity_lines)
    heads = heading_paths(corpus)
    catalog_chars = len(prompt_lines(catalog)) if catalog else 0
    prefix_tokens = int((len(BRIEF) + catalog_chars + len(outline_text(corpus))) / CHARS_PER_TOKEN) + 3000  # + tools
    chunk_tokens = [int(len(chunk_message(c, corpus, heads, cells)) / CHARS_PER_TOKEN) for c in chunks]
    price = config.price_for(config.model_for(EXTRACT))
    match = kalkyl.roles[MATCH]
    match_price = config.price_for(config.model_for(MATCH))

    def cost(output_per_chunk: int, repair_share: float) -> float:
        n = len(chunks)
        write = prefix_tokens * price.cache_write_5m
        reads = prefix_tokens * max(0, n - 1) * price.cache_read
        inputs = sum(chunk_tokens) * price.input
        outputs = n * output_per_chunk * price.output
        repairs = repair_share * (inputs + outputs)  # a repair turn resends the chunk and the first answer
        r3 = (prefix_tokens + 4000) * match_price.input + match.max_tokens / 2 * match_price.output
        cards = sources["read"] * CARD_USD_PER_TILE * 1_000_000
        return (write + reads + inputs + outputs + repairs + r3 + cards) / 1_000_000

    low, high = OUTPUT_TOKENS_PER_CHUNK
    extra: dict[str, Any] = {}
    if kalkyl.group_pass:
        groups, chars = group_bound(corpus, heads, cells)
        calls = READINGS * groups
        tokens = READINGS * chars / CHARS_PER_TOKEN
        pass_usd = (calls * prefix_tokens * price.cache_read + tokens * price.input + calls * high * price.output) / 1e6
        extra = {"group_pass_calls_max": calls, "group_pass_cost_usd_est_high": round(pass_usd, 2)}
    return {
        "chunks": len(chunks),
        "quantity_lines": sum(len(c.quantity_ids) for c in chunks),
        "scope_lines": sum(len(c.scope_ids) for c in chunks),
        "block_chars": sum(c.chars for c in chunks),
        "tiles": len(corpus.tiles),
        "cards_stored": sources["stored"],
        "cards_from_earlier_run": sources["earlier_run"],
        "cards_to_read": sources["read"],
        "prefix_tokens_est": prefix_tokens,
        "chunk_tokens_est_max": max(chunk_tokens, default=0),
        "cost_usd_est_low": round(cost(low, 0.1), 2),
        "cost_usd_est_high": round(cost(high, 0.5) + extra.get("group_pass_cost_usd_est_high", 0), 2),
        **extra,
    }
