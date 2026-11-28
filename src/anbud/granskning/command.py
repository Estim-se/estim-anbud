"""`anbud run <company> <tender>`: Granskning end to end on a prepared dev tender or fixture: page cards, map, lenses,
checks, merge, verifier, kronor, and the HTML and XLSX report in `<tender>/runs/<run_id>/report/`.

Prints ids, statuses, counts and costs only, never tender text or model output. A paid run reads the model key from
the Keychain; `--replay` answers every call from the run's step cache at $0 and refuses any call not in it.

A finished paid dev run is frozen: hashed into the freeze ledger, then its outputs folder and run record become
read-only (files 0o400, folders 0o500). A frozen run is never resumed or replayed in place. `--base-run <run>` rebuilds
from it under a new run id with its salt, so every unchanged call comes from its step cache ($0, and with `--replay`
nothing is sent; a miss exits 4); a finished rebuild is frozen the same way. An unfinished run stays writable, so the
same run id resumes it, and a fixture run is never frozen, so it still replays in place.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anbud import paths
from anbud.frozen import freeze_tree, is_frozen

PAID_REFUSED = 3
REPLAY_INCOMPLETE = 4
RUN_HALTED = 5
# Calls failed for good and no lens call was answered, by the model or the step cache, whatever happened to the map:
# nothing was reviewed. Never frozen; resume the run id once the cause is fixed. `anbud kalkyl` exits with it too when
# calls failed and no reading call was answered.
MODEL_UNREACHABLE = 6
# The run finished, but its own freeze didn't: no ledger line, or outputs that couldn't all be made read-only. The work
# is complete: the operator can finish the freeze by hand (`anbud freeze run`), and an exam's sealed freeze hashes and
# locks it itself, so the exam counts it as finished (`exam.Engines`). `anbud kalkyl` exits with it too.
FINISHED_NOT_FROZEN = 7
LENS_STEP = "lens:"  # every lens call's step starts so (`loop.run_lens`); the checks' steps don't


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--run-id", help="resume or replay an unfinished run (default: a new id from the tender and the time)"
    )
    parser.add_argument("--replay", action="store_true", help="answer from the step cache only, at $0")
    parser.add_argument(
        "--base-run",
        help="rebuild from this stored run under a new --run-id: its salt and step cache, unchanged calls $0",
    )
    parser.add_argument("--concurrency", type=int, default=3, help="lenses in parallel (default 3)")
    parser.add_argument("--lens", action="append", default=None, help="run only this lens (repeatable)")
    parser.add_argument(
        "--checks", action="store_true", help="with --lens, also run the focused checks (they run by default otherwise)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="build everything up to the first model call, print counts, call nothing"
    )
    parser.add_argument(
        "--arm", type=Path, help="one arm of a planted variant of a dev tender: <tender>/planted/seed-<N>/arms/<arm>"
    )


def checks_wanted(args: argparse.Namespace) -> bool:
    """The focused checks run by default; a run of selected lenses (`--lens`) runs them only with `--checks`."""
    return not args.lens or bool(args.checks)


def _dirs(args: argparse.Namespace, kind: str) -> tuple[Path, Path, Path]:
    """The prep folder, the calc folder, and the folder that holds this tender's runs. An arm of a planted variant
    brings its own prep and calc.xlsx and runs under the tender's runs/; its answer key is never read."""
    if getattr(args, "arm", None) is not None:
        from anbud.evals.planted.variant import resolve_arm

        tender = paths.dev_tender_dir(args.company_id, args.tender_id)
        arm_dir, args.arm_rel = resolve_arm(tender, args.arm)
        return arm_dir / "prep", arm_dir, tender / "runs"
    if kind == "fixture":
        fixture = args.fixture.expanduser().resolve()
        base = paths.fixture_data_dir(args.company_id, fixture)
        return base / "prep", fixture / "calc", base / "runs"
    tender = paths.dev_tender_dir(args.company_id, args.tender_id)
    return tender / "prep", tender / "calc", tender / "runs"


def run_command(args: argparse.Namespace, kind: str) -> int:
    from anbud.config import load_config
    from anbud.granskning import provenance
    from anbud.granskning.budget import PrefixOverBudget
    from anbud.granskning.calc import load_calc
    from anbud.granskning.catalog import load_named_catalog
    from anbud.granskning.lenses import load_lenses
    from anbud.granskning.pipeline import run_pipeline
    from anbud.granskning.prefix import estimate_tokens, system_blocks
    from anbud.granskning.run import DEGRADE_ROLES
    from anbud.granskning.settings import load_settings
    from anbud.model_client import Budget, KeyMissing, ModelClient, keychain_key, make_sdk_client
    from anbud.prep.tender import IntakeStop
    from anbud.selection import load_selected
    from anbud.stops import granskning_run_stop
    from anbud.store import RunStore, StoreError, check_run_id

    if getattr(args, "arm", None) is not None and kind != "dev":
        print("anbud run: --arm runs dev tenders only", file=sys.stderr)
        return PAID_REFUSED
    try:
        prep_dir, calc_dir, runs_dir = _dirs(args, kind)
    except ValueError as exc:
        print(f"anbud run: refused: {exc}", file=sys.stderr)
        return PAID_REFUSED
    arm = getattr(args, "arm_rel", None)
    if not (prep_dir / "blocks.jsonl").exists():
        print("anbud run: no prepared tender; run `anbud prep` first", file=sys.stderr)
        return PAID_REFUSED
    base_run = getattr(args, "base_run", None)
    if args.replay and not (args.run_id or base_run):
        print("anbud run: --replay needs --run-id or --base-run", file=sys.stderr)
        return PAID_REFUSED
    name = args.tender_id or "fixture"
    if arm:  # planted/seed-N/arms/<arm> -> <tender>-seed-N-<arm>
        name = f"{name}-{arm.split('/')[1]}-{arm.split('/')[3]}"
    run_id = args.run_id or f"{name}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    try:
        check_run_id(run_id)
        if base_run is not None:
            check_run_id(base_run)
    except StoreError as exc:
        print(f"anbud run: {exc}", file=sys.stderr)
        return PAID_REFUSED
    if base_run == run_id:
        print("anbud run: a rebuild needs its own --run-id", file=sys.stderr)
        return PAID_REFUSED

    config = load_config()
    lenses = load_lenses()
    if args.lens:
        unknown = set(args.lens) - {lens.id for lens in lenses}
        if unknown:
            print(f"anbud run: unknown lens: {', '.join(sorted(unknown))}", file=sys.stderr)
            return PAID_REFUSED
        lenses = [lens for lens in lenses if lens.id in args.lens]
    corpus, _ = load_selected(prep_dir)
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
        return dry_run(corpus, calc, lenses, catalog, config.budgets, code, args.company_id)
    store = RunStore(args.company_id)
    run_dir = runs_dir / run_id
    salt = None
    if base_run is not None:
        salt = store.stored_salt(base_run)
        if salt is None:
            print(f"anbud run: no stored run {base_run} to rebuild from", file=sys.stderr)
            return PAID_REFUSED
    in_place = args.replay and base_run is None  # replays an unfinished or fixture run into its replay-<n>/
    refusal = frozen_refusal(args.company_id, run_id)
    if refusal is None and (is_frozen(run_dir) or is_frozen(store.run_dir(run_id))):
        refusal = f"run {run_id} is frozen; start a new run id"
    if refusal is not None and in_place:
        refusal += f". Replay a frozen run as a rebuild: --replay --base-run {run_id} --run-id <new id>"
    if not args.replay:
        refusal = provenance.paid_refusal(state) or refusal
    if refusal is not None:
        print(f"anbud run: {refusal}", file=sys.stderr)
        return PAID_REFUSED
    sdk = None
    if in_place:
        run_dir = replay_dir(run_dir)  # never over the run it replays
    else:
        try:  # a new run's manifest records its code (a rebuild's, its base's salt); a resumed run keeps the first
            store.open_run(run_id, salt=salt, code=code, base_run=base_run)
        except StoreError as exc:
            print(f"anbud run: {exc}", file=sys.stderr)
            return PAID_REFUSED
    if not args.replay:
        try:
            sdk = make_sdk_client(config, keychain_key(account=config.workspace))
        except KeyMissing as exc:
            print(f"anbud run: {exc}", file=sys.stderr)
            return PAID_REFUSED
    # The dry run's tier, raised to its scaled floor, is the most the run may spend (the exam's cap is this stop); the
    # prefix the map's first call measures can only lower it, never below the floor.
    ceiling = granskning_run_stop(config, estimate_tokens(system_blocks(corpus, calc)), len(corpus.tiles))
    budget = Budget(ceiling.hard_stop, wait=True, floor_usd=ceiling.floor)  # a call waits for room (`Budget`)
    client = ModelClient(
        config,
        sdk,
        budget,
        run_id=run_id,
        company_id=args.company_id,
        store=store,
        salt=salt,
        degrade_roles=DEGRADE_ROLES,
    )
    try:
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
                summary_file=f"granskning-{run_dir.name}.json" if in_place else "granskning.json",
                code=code,
                catalog=catalog,
                settings=settings,
                concurrency=args.concurrency,
                on_progress=lambda msg: print(f"  {msg}", flush=True),
                arm=arm,
                checks=checks_wanted(args),
            )
        except (IntakeStop, PrefixOverBudget) as exc:
            print(f"anbud run: stopped: {exc}", file=sys.stderr)
            return PAID_REFUSED
        g = result.granskning
        how = "measured" if g.prefix_measured else "estimated"
        print(f"prefix {g.prefix_tokens} tokens ({how}); lens caps {g.caps}")
        if g.stop is not None:
            print(f"  hard stop: tier {g.stop.tier}, ${g.stop.hard_stop:.2f}, from the {g.stop.source} prefix")
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
        cov = result.finished.report.coverage
        print(
            f"  verifier: {len(result.verifier.outcomes)} flags {verdicts}, verified {cov.verifier_verified} of "
            f"{cov.verifier_selected}, ${result.verifier.cost_usd:.2f}"
        )
        if cov.lenses_lost or cov.map_lost or client.retries or client.failures:
            kinds = ", ".join(f"{k} {n}" for k, n in sorted(client.failures.items())) or "none"
            print(
                f"  degraded: {client.retries} attempts made again; calls failed for good: {kinds}; "
                f"map {'lost' if cov.map_lost else 'kept'}; lenses lost: {', '.join(cov.lenses_lost) or 'none'}"
            )
        if client.budget_refused:
            skipped = [lc.lens_id for lc in cov.lenses if lc.budget]
            print(
                f"  hard stop reached: {client.budget_refused} calls refused; lenses not finished: "
                f"{', '.join(skipped) or 'none'}; {cov.not_verified_budget} flags not verified (budget)"
            )
        for name, check in (result.checks.summary() if result.checks else {}).items():
            print(
                f"  {name}: {check['status']}, {check['flags']} flags, {check['turns']} turns, ${check['cost_usd']:.2f}"
            )
        ranked = result.finished.ranked
        print(
            f"  flags: {len(result.finished.items)} after merge; {len(ranked.shown)} shown, "
            f"{len(ranked.unpriced)} shown unpriced, {len(ranked.generic)} generic"
        )
        if result.catalog is not None:
            print(f"  catalog: {result.catalog.name}, hourly rate {result.catalog.hourly_rate:g}")
        print(f"total ${result.cost_usd:.2f}")
        print(f"report: {result.files['html'].parent}")
        if client.persist_failures:
            print(f"anbud run: {client.persist_failures} store writes failed", file=sys.stderr)
            return 1
        if client.replay_misses:
            print(
                f"anbud run: replay incomplete: {client.replay_misses} calls were not in the step cache",
                file=sys.stderr,
            )
            return REPLAY_INCOMPLETE
        if client.unanswered(LENS_STEP):
            kinds = ", ".join(f"{k} {n}" for k, n in sorted(client.failures.items()))
            print(
                f"anbud run: no lens call was answered (calls failed for good: {kinds}); not frozen. Check the key, "
                "the credit balance, the network and the API's status, then resume it with the same --run-id",
                file=sys.stderr,
            )
            return MODEL_UNREACHABLE
        if client.halted:
            print(
                f"anbud run: stopped before it finished ({client.halted} calls refused); not frozen. "
                "Resume it with the same --run-id",
                file=sys.stderr,
            )
            return RUN_HALTED
        return freeze_after_run(args, kind, run_id, run_dir, store.run_dir(run_id))
    finally:  # every exit, a crash too: the exam charges what this line says, or the whole tier when it's missing
        print(f"budget spent ${budget.spent:.2f} of ${budget.hard_stop:.2f}", flush=True)


def frozen_refusal(company_id: str, run_id: str) -> str | None:
    """Why a paid run may not use this id: a frozen run is a prediction that never changes, and resuming it would
    write over its read-only outputs and append to its hashed record."""
    from anbud.evals.freeze import FreezeError, frozen_line

    try:
        line = frozen_line(company_id, run_id)
    except FreezeError as exc:
        return f"can't check the freeze ledger: {exc}"
    except OSError as exc:
        return f"can't check the freeze ledger: {type(exc).__name__}"
    if line is None:
        return None
    return f"run {run_id} is frozen at ledger line {line}; start a new run id"


def freeze_after_run(args: argparse.Namespace, kind: str, run_id: str, outputs_dir: Path, record_dir: Path) -> int:
    """Hash a finished paid dev run into the freeze ledger, so it is provably blind before any answer arrives. A
    replay, a dry run or a fixture freezes nothing, and run_command only calls this for a run that finished: a run
    with failed store writes, a replay miss or a stop or cap exits non-zero before it. A run that can't be frozen
    exits FINISHED_NOT_FROZEN."""
    if kind != "dev" or args.dry_run or (args.replay and getattr(args, "base_run", None) is None):
        return 0
    from anbud.evals.freeze import FreezeError, freeze_run, read_only_warning

    try:
        frozen = freeze_run(
            args.company_id, run_id, outputs_dir=outputs_dir, run_record_dir=record_dir, tender_id=args.tender_id
        )
    except FreezeError as exc:
        print(f"anbud run: not frozen: {exc}", file=sys.stderr)
        return FINISHED_NOT_FROZEN
    except Exception as exc:  # the message may hold a path, so only the type is shown
        print(f"anbud run: not frozen: {type(exc).__name__}", file=sys.stderr)
        return FINISHED_NOT_FROZEN
    print(f"frozen: ledger line {frozen['line_number']}, {frozen['line_sha256'][:12]}")
    if (warning := read_only_warning(frozen)) is not None:
        print(f"anbud run: {warning}", file=sys.stderr)
        return FINISHED_NOT_FROZEN
    failed = freeze_tree(outputs_dir) + freeze_tree(record_dir)  # the folders too, so nothing can be added
    if failed:
        reasons = ", ".join(sorted(set(failed)))
        print(f"anbud run: frozen, but {len(failed)} entries could not be made read-only ({reasons})", file=sys.stderr)
        return FINISHED_NOT_FROZEN
    return 0


def dry_run(
    corpus: Any,
    calc: list[Any],
    lenses: list[Any],
    catalog: Any,
    budgets: dict[str, Any],
    code: dict[str, Any],
    company_id: str | None = None,
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
    from anbud.config import load_config
    from anbud.granskning.budget import PrefixBudget, plan_prefix
    from anbud.selection import excluded_counts
    from anbud.stops import granskning_run_stop

    sel = excluded_counts(corpus)
    print(
        f"  selection: {sel['documents_excluded']} documents of other trades left out ({sel['pages_excluded']} pages), "
        f"{sel['boundary_lines']} of their lines kept"
    )
    budget = PrefixBudget.from_config(budgets)
    stop = granskning_run_stop(load_config(), tokens, len(corpus.tiles))
    print(
        f"  hard stop: tier {stop.tier}, ${stop.hard_stop:.2f} at most, from the estimated prefix; a paid run lowers "
        "it when the prefix its first call measures is in a lower tier, never below its floor"
    )
    print(
        f"  scaled floor: ${stop.floor:.2f}, so page cards and the map take at most "
        f"{float(budgets['granskning']['card_map_share']):g} of the stop (tier's stop ${stop.usd:.2f})"
    )
    if budget is not None:
        plan = plan_prefix(corpus, calc, budget, corpus.tiles.values())
        print(
            f"  prefix budget {plan.budget} tokens: fixed {plan.p0}, priority 1 {plan.p1}, priority 2 {plan.p2}, "
            f"priority 3 {plan.p3}, other trades' lines {plan.boundary}; priority 1's page cards at most {plan.cards}"
        )
        if plan.stops:
            print(
                "  nothing fits: a paid run stops before any model call (the fixed part and every document's outline, "
                f"{plan.floor} tokens, with every document search-only)"
            )
        elif not plan.p1_fits:
            core = (
                f"its core, the AF and the contract, fits ({plan.p0 + plan.core + plan.core_cards} tokens)"
                if plan.core_fits
                else f"nor does its core, the AF and the contract ({plan.p0 + plan.core + plan.core_cards} tokens with "
                "their cards at most), so the review may be partial"
            )
            print(
                f"  priority 1 does not fit in full ({plan.p0 + plan.p1 + plan.cards} tokens with its cards at most); "
                f"{core}: {len(plan.p1_search_only)} priority-1 documents become search-only, "
                f"{len(plan.core_search_only)} of them the core's; planned prefix {plan.total} tokens before the cards "
                f"(at least {plan.floor}), {len(plan.search_only)} documents search-only in all, "
                f"{len(plan.boundary_dropped)} lines past the cap"
            )
        else:
            # P1 fits on its own, but with the other documents' outlines and the other trades' lines it may not
            demoted = (
                f"; yet {len(plan.p1_search_only)} priority-1 documents become search-only, "
                f"{len(plan.core_search_only)} of them the core's, to make room for the other documents' outlines and "
                "the other trades' lines"
                if plan.p1_search_only
                else ""
            )
            print(
                f"  priority 1 fits ({plan.p0 + plan.p1 + plan.cards} tokens with its cards at most){demoted}; planned "
                f"prefix {plan.total} tokens before the cards, {len(plan.search_only)} documents search-only, "
                f"{len(plan.boundary_dropped)} lines past the cap"
            )
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
    from anbud.granskning.check_dry import checks_line

    print(f"  checks: {checks_line(corpus, calc, company_id)}")
    print("  no model calls made")
    return 0


def replay_dir(run_dir: Path) -> Path:
    """`<run>/replay-<n>`, the first n not taken, so a replay never writes over the run it replays or an earlier
    replay."""
    n = 1
    while (run_dir / f"replay-{n}").exists():
        n += 1
    return run_dir / f"replay-{n}"
