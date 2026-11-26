"""`anbud run <company> <tender>`: Granskning on a prepared dev tender or fixture, up to the lenses.

Prints ids, statuses, counts and costs only, never tender text or model output. A paid run reads the model key from
the Keychain; `--replay` answers every call from the run's step cache at $0 and refuses any call not in it.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from anbud import paths

PAID_REFUSED = 3


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-id", help="resume or replay this run (default: a new id from the tender and the time)")
    parser.add_argument("--replay", action="store_true", help="answer from the step cache only, at $0")
    parser.add_argument("--concurrency", type=int, default=3, help="lenses in parallel (default 3)")
    parser.add_argument("--lens", action="append", default=None, help="run only this lens (repeatable)")


def _dirs(args: argparse.Namespace, kind: str) -> tuple[Path, Path]:
    if kind == "fixture":
        fixture = args.fixture.expanduser().resolve()
        return paths.company_dir(args.company_id) / "fixtures" / fixture.name / "prep", fixture / "calc"
    tender = paths.dev_tender_dir(args.company_id, args.tender_id)
    return tender / "prep", tender / "calc"


def run_command(args: argparse.Namespace, kind: str) -> int:
    from anbud.config import load_config
    from anbud.granskning.calc import load_calc
    from anbud.granskning.corpus import Corpus
    from anbud.granskning.lenses import load_lenses
    from anbud.granskning.run import run_granskning
    from anbud.granskning.toolbox import Toolbox
    from anbud.model_client import Budget, KeyMissing, ModelClient, keychain_key, make_sdk_client
    from anbud.store import RunStore, StoreError, check_run_id

    prep_dir, calc_dir = _dirs(args, kind)
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
    store = RunStore(args.company_id)
    if args.replay:
        sdk = None
    else:
        try:
            sdk = make_sdk_client(config, keychain_key(account=config.workspace))
        except KeyMissing as exc:
            print(f"anbud run: {exc}", file=sys.stderr)
            return PAID_REFUSED
    budget = Budget(float(config.budgets["run"]["hard_stop"]))
    client = ModelClient(config, sdk, budget, run_id=run_id, company_id=args.company_id, store=store)
    toolbox = Toolbox(corpus, calc, tile_loader=store.get_blob)
    print(f"run {run_id}: {len(corpus.blocks)} blocks, {len(calc)} calc posts, {len(lenses)} lenses")
    result = run_granskning(
        client,
        corpus,
        calc,
        lenses,
        toolbox=toolbox,
        concurrency=args.concurrency,
        on_progress=lambda msg: print(f"  {msg}", flush=True),
    )
    print(f"prefix about {result.prefix_tokens} tokens; lens caps {result.caps}")
    for lens in result.lenses:
        print(
            f"  {lens.lens_id}: {lens.status}, {len(lens.flags)} flags, {len(lens.rejected)} rejected, "
            f"{lens.turns} turns, ${lens.cost_usd:.2f}"
        )
    print(f"total ${result.cost_usd:.2f} (budget spent ${budget.spent:.2f} of ${budget.hard_stop:.2f})")
    print(f"results: {store.run_dir(run_id) / 'granskning.json'}")
    if client.persist_failures:
        print(f"anbud run: {client.persist_failures} store writes failed", file=sys.stderr)
        return 1
    return 0
