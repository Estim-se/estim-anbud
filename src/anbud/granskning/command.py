"""`anbud run <company> <tender>`: Granskning end to end on a prepared dev tender or fixture: page cards, map, lenses,
checks, merge, verifier, kronor, and the HTML and XLSX report in `<tender>/runs/<run_id>/report/`.

Prints ids, statuses, counts and costs only, never tender text or model output. A paid run reads the model key from
the Keychain; `--replay` answers every call from the run's step cache at $0 and refuses any call not in it.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anbud import paths

PAID_REFUSED = 3
REPLAY_INCOMPLETE = 4


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-id", help="resume or replay this run (default: a new id from the tender and the time)")
    parser.add_argument("--replay", action="store_true", help="answer from the step cache only, at $0")
    parser.add_argument("--concurrency", type=int, default=3, help="lenses in parallel (default 3)")
    parser.add_argument("--lens", action="append", default=None, help="run only this lens (repeatable)")
    parser.add_argument(
        "--dry-run", action="store_true", help="build everything up to the first model call, print counts, call nothing"
    )


def _dirs(args: argparse.Namespace, kind: str) -> tuple[Path, Path, Path]:
    """The prep folder, the calc folder, and the folder that holds this tender's runs."""
    if kind == "fixture":
        fixture = args.fixture.expanduser().resolve()
        base = paths.company_dir(args.company_id) / "fixtures" / fixture.name
        return base / "prep", fixture / "calc", base / "runs"
    tender = paths.dev_tender_dir(args.company_id, args.tender_id)
    return tender / "prep", tender / "calc", tender / "runs"


def run_command(args: argparse.Namespace, kind: str) -> int:
    from anbud.config import load_config
    from anbud.granskning import provenance
    from anbud.granskning.calc import load_calc
    from anbud.granskning.catalog import load_named_catalog
    from anbud.granskning.corpus import Corpus
    from anbud.granskning.lenses import load_lenses
    from anbud.granskning.pipeline import run_pipeline
    from anbud.granskning.settings import load_settings
    from anbud.model_client import Budget, KeyMissing, ModelClient, keychain_key, make_sdk_client
    from anbud.prep.tender import IntakeStop
    from anbud.store import RunStore, StoreError, check_run_id

    prep_dir, calc_dir, runs_dir = _dirs(args, kind)
    if not (prep_dir / "blocks.jsonl").exists():
        print("anbud run: no prepared tender; run `anbud prep` first", file=sys.stderr)
        return PAID_REFUSED
    if args.replay and not args.run_id:
        print("anbud run: --replay needs --run-id", file=sys.stderr)
        return PAID_REFUSED
    name = args.tender_id or "fixture"
    run_id = args.run_id or f"{name}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    try:
        check_run_id(run_id)
    except StoreError as exc:
        print(f"anbud run: {exc}", file=sys.stderr)
        return PAID_REFUSED

    config = load_config()
    lenses = load_lenses()
    if args.lens:
        unknown = set(args.lens) - {lens.id for lens in lenses}
        if unknown:
            print(f"anbud run: unknown lens: {', '.join(sorted(unknown))}", file=sys.stderr)
            return PAID_REFUSED
        lenses = [lens for lens in lenses if lens.id in args.lens]
    corpus = Corpus.load(prep_dir)
    calc = load_calc(calc_dir)
    settings = load_settings()
    try:
        catalog = load_named_catalog(settings.default_catalog)
    except FileNotFoundError:
        catalog = None
        print("anbud run: no catalog file; kronor prices contract formulas and calc posts only", file=sys.stderr)
    state = provenance.git_state()
    code = provenance.code_block(state)
    if args.dry_run:
        return dry_run(corpus, calc, lenses, catalog, config.budgets, code)
    store = RunStore(args.company_id)
    run_dir = runs_dir / run_id
    if args.replay:
        sdk = None
        run_dir = replay_dir(run_dir)  # never over the run it replays
    else:
        refusal = provenance.paid_refusal(state)
        if refusal is not None:
            print(f"anbud run: {refusal}", file=sys.stderr)
            return PAID_REFUSED
        store.open_run(run_id, code=code)  # a new run's manifest records its code; a resumed run keeps the first
        try:
            sdk = make_sdk_client(config, keychain_key(account=config.workspace))
        except KeyMissing as exc:
            print(f"anbud run: {exc}", file=sys.stderr)
            return PAID_REFUSED
    budget = Budget(float(config.budgets["run"]["hard_stop"]))
    client = ModelClient(config, sdk, budget, run_id=run_id, company_id=args.company_id, store=store)
    counts = f"{len(corpus.blocks)} blocks, {len(corpus.tiles)} tiles, {len(calc)} calc posts, {len(lenses)} lenses"
    print(f"run {run_id}: {counts}")
    try:
        result = run_pipeline(
            client,
            store,
            corpus,
            calc,
            lenses,
            tender_id=args.tender_id or args.fixture.expanduser().resolve().name,
            prep_dir=prep_dir,
            run_dir=run_dir,
            summary_file=f"granskning-{run_dir.name}.json" if args.replay else "granskning.json",
            code=code,
            catalog=catalog,
            settings=settings,
            concurrency=args.concurrency,
            on_progress=lambda msg: print(f"  {msg}", flush=True),
        )
    except IntakeStop as exc:
        print(f"anbud run: stopped: {exc}", file=sys.stderr)
        return PAID_REFUSED
    g = result.granskning
    how = "measured" if g.prefix_measured else "estimated"
    print(f"prefix {g.prefix_tokens} tokens ({how}); lens caps {g.caps}")
    print(f"  map: {g.map.outcome.status}, ${g.map.outcome.cost_usd:.2f}")
    for lens in g.lenses:
        error = f" ({lens.error})" if lens.error else ""
        print(
            f"  {lens.lens_id}: {lens.status}{error}, {len(lens.flags)} flags, {len(lens.rejected)} rejected, "
            f"{lens.turns} turns, ${lens.cost_usd:.2f}"
        )
    verdicts: dict[str, int] = {}
    for outcome in result.verifier.outcomes.values():
        verdicts[outcome.status] = verdicts.get(outcome.status, 0) + 1
    print(f"  verifier: {len(result.verifier.outcomes)} flags {verdicts}, ${result.verifier.cost_usd:.2f}")
    ranked = result.finished.ranked
    print(
        f"  flags: {len(result.finished.items)} after merge; {len(ranked.shown)} shown, {len(ranked.generic)} generic"
    )
    if result.catalog is not None:
        print(f"  catalog: {result.catalog.name}, hourly rate {result.catalog.hourly_rate:g}")
    print(f"total ${result.cost_usd:.2f} (budget spent ${budget.spent:.2f} of ${budget.hard_stop:.2f})")
    print(f"report: {result.files['html'].parent}")
    if client.persist_failures:
        print(f"anbud run: {client.persist_failures} store writes failed", file=sys.stderr)
        return 1
    if client.replay_misses:
        print(f"anbud run: replay incomplete: {client.replay_misses} calls were not in the step cache", file=sys.stderr)
        return REPLAY_INCOMPLETE
    return 0


def dry_run(
    corpus: Any, calc: list[Any], lenses: list[Any], catalog: Any, budgets: dict[str, Any], code: dict[str, Any]
) -> int:
    """Counts for a run that makes no model call: the prefix as the map would send it before page cards add their
    blocks, the tiles the cards would read, and the lens tier the estimate picks. No key, no store writes."""
    from anbud.granskning.catalog import calc_hourly_rate
    from anbud.granskning.prefix import CHARS_PER_TOKEN, estimate_tokens, system_blocks
    from anbud.granskning.run import lens_caps

    blocks = system_blocks(corpus, calc)
    sizes = [len(b["text"]) for b in blocks]
    tokens = estimate_tokens(blocks)
    caps = lens_caps(budgets, tokens)
    print(f"dry run: {len(corpus.blocks)} blocks, {len(corpus.documents)} documents, {len(calc)} calc posts")
    print(f"  system blocks: {len(blocks)}, chars {sizes} = {sum(sizes)}")
    print(
        f"  estimated prefix: {tokens} tokens at {CHARS_PER_TOKEN} chars/token, before page cards add their text; "
        "a paid run measures it on the first call"
    )
    print(f"  tiles for page cards: {len(corpus.tiles)}")
    print(
        f"  lens tier (from the estimate): {caps.max_turns} turns, ${caps.max_usd:.2f} per lens; {len(lenses)} lenses"
    )
    dirty = f"dirty, diff {code['diff_sha256'][:12]}" if code["dirty"] and code["diff_sha256"] else "clean"
    sha = (code["harness_sha"] or "none")[:12]
    paid = "allowed" if code["harness_sha"] and not code["dirty"] else "refused on this tree"
    print(f"  harness {sha}, {dirty}; a paid run is {paid}")
    rate = calc_hourly_rate(calc)
    if catalog is None:
        print("  catalog: none")
    else:
        source = "calc" if rate is not None else "catalog"
        print(f"  catalog: {catalog.name}, {len(catalog.items)} items, hourly rate from the {source}")
    print("  no model calls made")
    return 0


def replay_dir(run_dir: Path) -> Path:
    """`<run>/replay-<n>`, the first n not taken, so a replay never writes over the run it replays or an earlier
    replay."""
    n = 1
    while (run_dir / f"replay-{n}").exists():
        n += 1
    return run_dir / f"replay-{n}"
