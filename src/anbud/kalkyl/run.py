"""`anbud kalkyl`: a dev tender's prepared blocks to a priced calc in the tender's data folder.

Reads `<tender>/prep/` (and `prep/page_cards.jsonl`, tile_text blocks from R1, when present) and the catalog from
`~/estim-anbud-data/catalogs/<name>.json`. Writes, under `<tender>/kalkyl/<run_id>/`:

    kalkyl.xlsx    the calc, Swedish labels, catalog label on every sheet
    posts.json     list[CalcPost], the shared contract
    result.json    posts, records, object model and uncovered lines with their citations (customer data, stays here)
    summary.json   counts and cost only

`plan()` is the $0 dry run: chunk counts and a cost estimate, with no model call.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from anbud import paths
from anbud.config import Config, load_config
from anbud.granskning.corpus import Corpus
from anbud.kalkyl.catalog import Catalog, CatalogError, load_catalog
from anbud.kalkyl.client import (
    EXTRACT,
    MATCH,
    KalkylClient,
    KalkylConfig,
    extract_tools,
    load_kalkyl_config,
    match_tools,
)
from anbud.kalkyl.extract import BRIEF, chunk_message, outline_text
from anbud.kalkyl.lines import heading_paths, make_chunks
from anbud.kalkyl.pipeline import KalkylResult, run_kalkyl
from anbud.kalkyl.price import calc_posts
from anbud.kalkyl.xlsx import write_workbook
from anbud.model_client import Budget
from anbud.models import Block
from anbud.store import RunStore, canonical

CHARS_PER_TOKEN = 3.0  # Swedish text with block markup; conservative, so the estimate errs high
OUTPUT_TOKENS_PER_CHUNK = (3000, 8000)  # records plus adaptive thinking at medium effort, low and high


class KalkylError(RuntimeError):
    """Raised with a reason that names no tender content."""


def load_corpus(prep_dir: Path) -> Corpus:
    if not (prep_dir / "blocks.jsonl").exists():
        raise KalkylError("the tender has no prep output; run `anbud prep` first")
    corpus = Corpus.load(prep_dir)
    cards = prep_dir / "page_cards.jsonl"
    if cards.exists():
        for line in cards.read_text().splitlines():
            if not line:
                continue
            block = Block.model_validate_json(line)
            if block.kind == "tile_text" and block.id not in corpus.blocks:
                corpus.blocks[block.id] = block
                corpus.order.append(block.id)
    return corpus


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
            }
        )
    body = {
        "complete": result.complete,
        "totals": vars(result.totals),
        "objects": [vars(o) for o in result.objects.values()],
        "posts": posts,
        "records": [v.model_dump(mode="json") for v in result.verified],
        "rejected": [r.model_dump(mode="json") for r in result.rejected],
        "uncovered": [u.model_dump(mode="json") for u in result.uncovered],
        "chunks": [
            {"id": r.chunk_id, "status": r.status, "turns": r.turns, "cost_usd": r.cost_usd, "error": r.error}
            for r in result.chunk_results
        ],
    }
    return json.dumps(body, ensure_ascii=False, indent=1, default=_decimal).encode()


def new_run_id(tender_id: str) -> str:
    return f"kalkyl-{tender_id}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


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
) -> dict[str, Any]:
    """Run Kalkyl on a dev tender (the caller has checked it is one) and write the outputs. Returns counts only.
    With `sdk=None` the run is a replay: every answer must come from the run's step cache."""
    config = config or load_config()
    kalkyl = kalkyl or load_kalkyl_config()
    tender = paths.dev_tender_dir(company_id, tender_id, root)
    corpus = load_corpus(tender / "prep")
    catalog = load_catalog(catalog_name or kalkyl.default_catalog, root)
    store = RunStore(company_id, root)
    run_id = run_id or new_run_id(tender_id)
    budget = Budget(kalkyl.hard_stop)
    client = KalkylClient(
        config,
        sdk,
        budget,
        kalkyl=kalkyl,
        role_tools={EXTRACT: extract_tools(catalog.ids), MATCH: match_tools(catalog.ids)},
        run_id=run_id,
        company_id=company_id,
        store=store,
    )
    result = run_kalkyl(corpus, catalog, client, kalkyl)
    meta = {"company_id": company_id, "tender_id": tender_id, "run_id": run_id, "cost_usd": budget.spent}
    workbook, rows = write_workbook(result, corpus, catalog, meta)
    result.calc_posts = calc_posts(result.posts, rows)
    out = tender / "kalkyl" / run_id
    store.write_file(out / "kalkyl.xlsx", workbook)
    store.write_file(out / "posts.json", canonical([p.model_dump(mode="json") for p in result.calc_posts]))
    store.write_file(out / "result.json", result_json(result))
    summary = {
        "run_id": run_id,
        "catalog": catalog.name,
        **result.counts(),
        "cost_usd": round(budget.spent, 4),
        "persist_failures": client.persist_failures,
        "output": str(out),
    }
    store.write_file(out / "summary.json", json.dumps(summary, indent=2, sort_keys=True).encode())
    return summary


def plan(company_id: str, tender_id: str, *, catalog_name: str | None = None, root: Path | None = None) -> dict:
    """The $0 dry run: what a run would send, in counts, and a cost range from config prices. No model is called."""
    config = load_config()
    kalkyl = load_kalkyl_config()
    corpus = load_corpus(paths.dev_tender_dir(company_id, tender_id, root) / "prep")
    try:
        catalog: Catalog | None = load_catalog(catalog_name or kalkyl.default_catalog, root)
    except CatalogError:
        catalog = None
    chunks = make_chunks(corpus, kalkyl.chunk_max_chars, kalkyl.chunk_max_quantity_lines)
    heads = heading_paths(corpus)
    catalog_chars = len(catalog.prompt_lines()) if catalog else 0
    prefix_tokens = int((len(BRIEF) + catalog_chars + len(outline_text(corpus))) / CHARS_PER_TOKEN) + 3000  # + tools
    chunk_tokens = [int(len(chunk_message(c, corpus, heads)) / CHARS_PER_TOKEN) for c in chunks]
    settings = kalkyl.roles[EXTRACT]
    price = config.price_for(config.models[settings.model])
    match = kalkyl.roles[MATCH]
    match_price = config.price_for(config.models[match.model])

    def cost(output_per_chunk: int, repair_share: float) -> float:
        n = len(chunks)
        write = prefix_tokens * price.cache_write_5m
        reads = prefix_tokens * max(0, n - 1) * price.cache_read
        inputs = sum(chunk_tokens) * price.input
        outputs = n * output_per_chunk * price.output
        repairs = repair_share * (inputs + outputs)  # a repair turn resends the chunk and the first answer
        r3 = (prefix_tokens + 4000) * match_price.input + match.max_tokens / 2 * match_price.output
        return (write + reads + inputs + outputs + repairs + r3) / 1_000_000

    low, high = OUTPUT_TOKENS_PER_CHUNK
    return {
        "chunks": len(chunks),
        "quantity_lines": sum(len(c.quantity_ids) for c in chunks),
        "block_chars": sum(c.chars for c in chunks),
        "catalog": catalog.name if catalog else "missing",
        "catalog_items": len(catalog.items) if catalog else 0,
        "prefix_tokens_est": prefix_tokens,
        "chunk_tokens_est_max": max(chunk_tokens, default=0),
        "cost_usd_est_low": round(cost(low, 0.1), 2),
        "cost_usd_est_high": round(cost(high, 0.5), 2),
        "hard_stop_usd": kalkyl.hard_stop,
    }
