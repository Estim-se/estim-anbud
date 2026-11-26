"""The `anbud` command."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from anbud.runguard import RunRefused, check_run_allowed, held_out_refusal

NOT_BUILT = 2
REFUSED = 3
REPLAY_INCOMPLETE = 4  # a replay with calls not in the step cache, as `anbud run --replay`


def _run(args: argparse.Namespace) -> int:
    try:
        kind = check_run_allowed(args.company_id, args.tender_id, args.fixture)
    except RunRefused as exc:
        print(f"anbud run: refused: {exc}", file=sys.stderr)
        return REFUSED
    # Granskning end to end: page cards, map, lenses, checks, merge, verifier, kronor and the reports.
    from anbud.granskning.command import run_command

    return run_command(args, kind)


def _eval(args: argparse.Namespace) -> int:
    if args.kind in ("heldout", "prospective"):
        refusal = held_out_refusal()
        if refusal:
            print(f"anbud eval {args.kind}: refused: {refusal}", file=sys.stderr)
            return REFUSED
    print(f"anbud eval {args.kind}: not built yet", file=sys.stderr)
    return NOT_BUILT


def _prep(args: argparse.Namespace) -> int:
    from anbud import paths
    from anbud.prep.sources import SourceError
    from anbud.prep.tender import FIXTURE_EXCLUDE, PrepError, prepare, write_cells_sidecar
    from anbud.store import StoreError

    try:
        kind = check_run_allowed(args.company_id, args.tender_id, args.fixture)
    except RunRefused as exc:
        print(f"anbud prep: refused: {exc}", file=sys.stderr)
        return REFUSED
    try:
        if kind == "fixture":
            inbox = args.fixture.expanduser().resolve()
            out = paths.fixture_data_dir(args.company_id, inbox) / "prep"
            manifest, exclude = None, FIXTURE_EXCLUDE
        else:
            tender = paths.dev_tender_dir(args.company_id, args.tender_id)
            inbox, out, manifest, exclude = tender / "inbox", tender / "prep", tender / "inbox.sha256", ()
            if not inbox.resolve().is_relative_to(tender.resolve()):
                raise PrepError("the inbox resolves outside the tender's folder")
        if args.cells_only:  # the sidecar alone; blocks.jsonl and the rest stay byte for byte
            summary = write_cells_sidecar(args.company_id, inbox, out, manifest)
        else:
            summary = prepare(args.company_id, inbox, out, manifest, exclude=exclude)
    except (PrepError, SourceError, StoreError) as exc:
        print(f"anbud prep: stopped: {exc}", file=sys.stderr)
        return REFUSED
    except (OSError, ValueError) as exc:  # the message may hold a path, so only the type is shown
        print(f"anbud prep: stopped: {type(exc).__name__} while reading the tender", file=sys.stderr)
        return REFUSED
    for key, value in summary.items():
        print(f"{key}: {value}")
    return 0


def _intake(args: argparse.Namespace) -> int:
    from anbud.evals.split_heldout import SplitError
    from anbud.intake import IntakeError, intake, survey, training_cutoff

    try:
        if args.dry_run:
            result, _ = survey(args.incoming.expanduser())
            print(f"anbud intake (dry run, nothing written or split): {result.notices.tenders} zips")
            for line in intake_report(result, training_cutoff())[2:]:
                print(line)
            return 0
        result = intake(
            args.company_id, args.incoming.expanduser(), new_ledger=args.new_ledger, company_name=args.company_name
        )
    except Exception as exc:  # noqa: BLE001 - a traceback or message could name a file: only ours are printed
        message = str(exc) if isinstance(exc, IntakeError | SplitError) else type(exc).__name__
        print(f"anbud intake: stopped: {message}", file=sys.stderr)
        return REFUSED
    for line in intake_report(result, training_cutoff() if result.notices.tenders else None):
        print(line)
    if result.failed:
        print(f"anbud intake: {len(result.failed)} not placed: {'; '.join(result.failed)}", file=sys.stderr)
        print(
            "anbud intake: each one is back in the incoming folder, or the next intake puts it back; run intake again",
            file=sys.stderr,
        )
        return 1
    return 0


def intake_report(result: Any, cutoff: Any = None) -> list[str]:
    """Counts only. The notice lines appear for a blind intake of zips."""
    u, n = result.unpack, result.notices
    lines = [
        f"anbud intake: {result.dev} dev, {result.heldout} held-out, {result.prospective} prospective, "
        f"{result.burned} burned, {result.already_in} already taken in",
        f"  zips: {u.archives_unpacked} unpacked, {u.archives_kept} kept as delivered; {u.entries} entries, "
        f"{u.entries_skipped} skipped (links, escaping paths, hidden or system files)",
    ]
    if result.rows_without_folder:
        lines.append(f"  metadata.csv: {result.rows_without_folder} rows name no folder here and no registered tender")
    if n.tenders:
        found = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in n.found.items())
        lines += [
            f"  notices: {n.tenders} read, {n.without_page} without a notice page; found {found}",
            f"  split keys: {n.no_entity_key} without a buyer key, {n.no_key} without any key (split alone)",
            f"  training cutoff {cutoff}: {n.before_cutoff} published before, {n.after_cutoff} after, "
            f"{n.unknown_date} unknown; {n.framework} framework agreements",
            f"  prospective by deadline: {n.open} still open, {n.no_deadline} with no deadline read, "
            f"{n.may_have_moved} passed lately with amendments or answers that may have moved it; "
            f"{n.deadline_from_pdf} deadlines read from PDFs",
        ]
        held = ", ".join(f"{k} {v}" for k, v in sorted(result.held.items())) or "none"
        lines.append(
            f"  held as answer keys: {sum(result.held.values())} files in {result.tenders_with_held} tenders ({held})"
        )
        if result.tenders_mostly_held:
            lines.append(f"  {result.tenders_mostly_held} dev tenders had more than half their files held")
    return lines


def _kalkyl(args: argparse.Namespace) -> int:
    """Kalkyl in shadow on a dev tender. Paid unless --dry-run or --replay; never in CI."""
    import os

    from anbud.config import CONFIG_DIR, load_config
    from anbud.kalkyl.catalog import CatalogError
    from anbud.kalkyl.run import KalkylError, plan, run_dev_tender
    from anbud.model_client import KeyMissing, keychain_key, make_sdk_client
    from anbud.prep.tender import IntakeStop
    from anbud.store import StoreError

    try:
        if check_run_allowed(args.company_id, args.tender_id) != "dev":
            raise RunRefused("only dev tenders")
    except RunRefused as exc:
        print(f"anbud kalkyl: refused: {exc}", file=sys.stderr)
        return REFUSED
    try:
        if args.replay and not (args.run_id or args.base_run):
            print("anbud kalkyl: --replay needs the --run-id or --base-run of a stored run", file=sys.stderr)
            return REFUSED
        if args.dry_run:
            summary = plan(args.company_id, args.tender_id, catalog_name=args.catalog)
        else:
            from anbud.granskning import provenance

            state = provenance.git_state()
            code = provenance.code_block(state)
            code["kalkyl_config_sha256"] = provenance._sha((CONFIG_DIR / "kalkyl.toml").read_bytes())
            sdk = None
            if not args.replay:
                if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
                    print("anbud kalkyl: refused: paid calls never run in CI", file=sys.stderr)
                    return REFUSED
                refusal = provenance.paid_refusal(state)
                if refusal:
                    print(f"anbud kalkyl: refused: {refusal}", file=sys.stderr)
                    return REFUSED
                config = load_config()
                sdk = make_sdk_client(config, keychain_key(account=config.workspace))
            summary = run_dev_tender(
                args.company_id,
                args.tender_id,
                sdk=sdk,
                catalog_name=args.catalog,
                run_id=args.run_id,
                code=code,
                base_run=args.base_run,
            )
    except (KalkylError, CatalogError, StoreError, KeyMissing, IntakeStop) as exc:
        print(f"anbud kalkyl: stopped: {exc}", file=sys.stderr)
        return REFUSED
    for key, value in summary.items():
        print(f"{key}: {value}")
    if summary.get("replay_misses"):
        misses = summary["replay_misses"]
        print(f"anbud kalkyl: replay incomplete: {misses} calls not in the step cache", file=sys.stderr)
        return REPLAY_INCOMPLETE
    if summary.get("freeze_failures"):
        print(f"anbud kalkyl: {summary['freeze_failures']} entries could not be made read-only", file=sys.stderr)
        return 1
    return 0


def _baseline(args: argparse.Namespace) -> int:
    """The app's current engine on a dev tender. Paid unless --dry-run; never in CI. Prints counts, costs and timings
    only; the engine's own output stays in the run record's engine.log."""
    import os

    from anbud import paths
    from anbud.evals import baseline, freeze
    from anbud.granskning import provenance
    from anbud.model_client import KeyMissing
    from anbud.store import StoreError

    try:
        if check_run_allowed(args.company_id, args.tender_id) != "dev":
            raise RunRefused("only dev tenders")
    except RunRefused as exc:
        print(f"anbud baseline: refused: {exc}", file=sys.stderr)
        return REFUSED
    state = provenance.git_state()
    if not args.dry_run:
        if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
            print("anbud baseline: refused: paid calls never run in CI", file=sys.stderr)
            return REFUSED
        refusal = provenance.paid_refusal(state)
        if refusal:
            print(f"anbud baseline: refused: {refusal}", file=sys.stderr)
            return REFUSED
    root = paths.data_root()
    try:
        result = baseline.run_dev(
            args.company_id,
            args.tender_id,
            dry_run=args.dry_run,
            hard_stop=args.hard_stop,
            app=(args.app or baseline.APP_DIR).expanduser(),
            app_ref=args.app_ref or baseline.APP_REF,
            code=provenance.code_block(state),
            stop_requested=lambda record: (root / "STOP").exists() or (record / "STOP").exists(),
        )
    except (baseline.BaselineError, KeyMissing, StoreError, ValueError) as exc:
        print(f"anbud baseline: stopped: {exc}", file=sys.stderr)
        return REFUSED
    for line in baseline_report(result):
        print(line)
    if result.outcome != "ok" or args.dry_run:
        return 0 if result.outcome == ("dry_run" if args.dry_run else "ok") else 1
    try:
        record = freeze.freeze_run(
            args.company_id,
            result.run_id,
            outputs_dir=paths.dev_tender_dir(args.company_id, args.tender_id) / "baseline" / result.run_id,
            run_record_dir=freeze.run_record_dir(args.company_id, result.run_id),
            tender_id=args.tender_id,
        )
    except freeze.FreezeError as exc:
        print(f"anbud baseline: not frozen: {exc}", file=sys.stderr)
        return 1
    print(f"frozen: ledger line {record['line_number']}, {record['line_sha256'][:12]}")
    if (warning := freeze.read_only_warning(record)) is not None:
        print(f"anbud baseline: {warning}", file=sys.stderr)
        return 1
    return 0


def baseline_report(result: Any) -> list[str]:
    """Counts, costs, timings and ids. No file name and nothing the engine wrote."""
    skipped = ", ".join(f"{k} {v}" for k, v in result.skipped_by_type.items()) or "none"
    lines = [
        f"anbud baseline: {result.outcome} (exit {result.exit_code}), run {result.run_id}",
        f"documents: {result.documents} given to the engine; skipped by type: {skipped}",
        f"calls: {result.calls} ({result.calls_failed} failed, {result.calls_incomplete} cut off), "
        f"cost ${result.cost_usd:.2f}, {result.duration_s:.0f} s",
        f"workspace ids: {', '.join(result.workspace_ids) or 'none seen'}; "
        f"organization ids: {len(result.organization_ids)} distinct",
        f"child environment: {', '.join(result.env_names) or 'not recorded'}",
    ]
    if result.outcome == "hard_stop":
        lines.append(f"hard stop: up to ${result.in_flight_usd_at_stop:.2f} was in flight when the run ended")
    if result.blocked_hosts:
        lines.append(f"blocked: {result.blocked_hosts} request to a host other than the Messages API")
    return lines


def _cards(args: argparse.Namespace) -> int:
    from anbud.evals.cards import CardError, write

    try:
        target, cards = write(args.company_id)
    except (CardError, ValueError) as exc:
        print(f"anbud cards: stopped: {exc}", file=sys.stderr)
        return REFUSED
    split = cards["split"]
    print(
        f"anbud cards: {split['dev']} dev cards, {split['heldout']} held-out and {split['prospective']} prospective "
        f"counted, written to notes/{args.company_id}/{target.name}"
    )
    return 0


def _stop(args: argparse.Namespace) -> int:
    from anbud.store import StoreError, request_stop

    one_run = bool(args.company_id and args.run_id)
    if args.all == one_run or (args.all and (args.company_id or args.run_id)):
        print("anbud stop: give a company id and a run id, or --all alone", file=sys.stderr)
        return REFUSED
    try:
        request_stop(args.company_id, args.run_id, clear=args.clear)
    except (StoreError, ValueError) as exc:
        print(f"anbud stop: {exc}", file=sys.stderr)
        return REFUSED
    target = "every run" if args.all else f"{args.company_id}/{args.run_id}"
    print(f"anbud stop: {'cleared' if args.clear else 'stop marker written'} for {target}")
    return 0


def _labels_tender_dir(name: str, args: argparse.Namespace) -> Path | None:
    """The data folder of a dev tender or a synthetic fixture, or None after printing why not."""
    from anbud import paths

    try:
        kind = check_run_allowed(args.company_id, args.tender_id, args.fixture)
    except RunRefused as exc:
        print(f"anbud {name}: refused: {exc}", file=sys.stderr)
        return None
    if kind == "fixture":
        return paths.fixture_data_dir(args.company_id, args.fixture)
    return paths.dev_tender_dir(args.company_id, args.tender_id)


# `anbud export-review`: a blind XLSX for a human labeller (plan §8.2).
def _export_review(args: argparse.Namespace) -> int:
    from anbud.granskning.labels import LabelError, export_review
    from anbud.store import StoreError

    tender = _labels_tender_dir("export-review", args)
    if tender is None:
        return REFUSED
    try:
        summary = export_review(args.company_id, tender, args.flags, args.export_id)
    except (LabelError, StoreError, OSError, ValueError) as exc:
        print(f"anbud export-review: stopped: {exc}", file=sys.stderr)
        return REFUSED
    for key, value in summary.items():
        print(f"{key}: {value}")
    return 0


# `anbud import-labels`: the filled XLSX back, as labels keyed by fingerprint in the tender's data folder.
def _import_labels(args: argparse.Namespace) -> int:
    from anbud.granskning.labels import LabelError, import_labels
    from anbud.store import StoreError

    tender = _labels_tender_dir("import-labels", args)
    if tender is None:
        return REFUSED
    try:
        summary = import_labels(args.company_id, tender, args.xlsx, args.labeller)
    except (LabelError, StoreError, OSError, ValueError) as exc:
        print(f"anbud import-labels: stopped: {exc}", file=sys.stderr)
        return REFUSED
    for key, value in summary.items():
        print(f"{key}: {value}")
    return 0


# `anbud regress`: are the confirmed catches in the tender's regression set still surfaced by a run?
def _regress(args: argparse.Namespace) -> int:
    from anbud import paths
    from anbud.granskning.regress import regress
    from anbud.store import StoreError, check_run_id

    try:
        if check_run_allowed(args.company_id, args.tender_id) != "dev":
            raise RunRefused("regress takes dev tenders only")
        check_run_id(args.run_id)
    except (RunRefused, StoreError) as exc:
        print(f"anbud regress: refused: {exc}", file=sys.stderr)
        return REFUSED
    tender = paths.dev_tender_dir(args.company_id, args.tender_id)
    try:
        result, out = regress(args.company_id, tender, args.run_id)
    except (OSError, ValueError, StoreError) as exc:  # pydantic's ValidationError is a ValueError
        print(f"anbud regress: stopped: {type(exc).__name__}", file=sys.stderr)
        return REFUSED
    n = result["entries"]
    print(f"surfaced: {result['surfaced']} of {n} ({'pass' if result['passed'] else 'FAIL'})")
    print(f"found: {result['found']} of {n}")
    print(f"not surfaced: {result['not_surfaced']}")
    print(f"not found: {result['not_found']}")
    print(f"result: {out}")
    return 0 if result["passed"] else 1


# `anbud export-recording`: a fixture run's step cache and outputs into the repo, for $0 replay in CI.
def _export_recording(args: argparse.Namespace) -> int:
    from anbud.granskning.recording import RecordingError, export_recording
    from anbud.store import RunStore, StoreError, check_run_id

    try:
        if check_run_allowed(args.company_id, None, args.fixture) != "fixture":
            raise RunRefused("only synthetic fixtures are recorded")
        check_run_id(args.run_id)
        summary = export_recording(RunStore(args.company_id), args.fixture, args.run_id)
    except (RunRefused, RecordingError, StoreError, OSError, ValueError) as exc:
        print(f"anbud export-recording: refused: {exc}", file=sys.stderr)
        return REFUSED
    for key, value in summary.items():
        print(f"{key}: {value}")


def _freeze(args: argparse.Namespace) -> int:
    from anbud import paths
    from anbud.evals import freeze

    try:
        if args.action == "verify":
            summary = freeze.verify(freeze.ledger_file(args.company_id))
            kinds = ", ".join(f"{kind} {n}" for kind, n in summary.kinds.items()) or "none"
            last = summary.last_sha256[:12] if summary.last_sha256 else "none"
            print(f"ledger: {summary.lines} lines, last {last}; {len(summary.tenders)} tenders frozen; kinds: {kinds}")
            return 0
        if args.action == "import":
            n = freeze.import_manual(args.company_id, args.file.expanduser())
            print(f"imported: {n} lines" if n else "imported: 0 lines (already at the start of the ledger)")
            return 0
        tender = args.tender
        if args.outputs is not None:
            outputs = args.outputs.expanduser()
        elif tender is not None:
            outputs = paths.dev_tender_dir(args.company_id, tender) / "runs" / args.run_id
        else:
            outputs, tender = freeze.find_run_outputs(args.company_id, args.run_id)
        record = freeze.freeze_run(
            args.company_id,
            args.run_id,
            outputs_dir=outputs,
            run_record_dir=freeze.run_record_dir(args.company_id, args.run_id),
            tender_id=tender,
        )
    except freeze.FreezeError as exc:
        print(f"anbud freeze {args.action}: refused: {exc}", file=sys.stderr)
        return REFUSED
    except Exception as exc:  # the message may hold a path, so only the type is shown
        print(f"anbud freeze {args.action}: refused: {type(exc).__name__}", file=sys.stderr)
        return REFUSED
    print(f"frozen: ledger line {record['line_number']}, {record['line_sha256'][:12]}")
    if (warning := freeze.read_only_warning(record)) is not None:
        print(f"anbud freeze run: {warning}", file=sys.stderr)
        return 1
    return 0


def _answers(args: argparse.Namespace) -> int:
    from anbud.evals import answers

    try:
        if args.action == "add":
            result = answers.add(args.company_id, args.tender_id, args.files, args.kind)
            short = ", ".join(sha[:12] for sha in result.sha256)
            print(f"answers: {result.added} added, {result.skipped} already stored ({args.kind}): {short}")
            return 0
        state = answers.status(args.company_id, args.tender_id)
    except answers.AnswerError as exc:
        print(f"anbud answers {args.action}: refused: {exc}", file=sys.stderr)
        return REFUSED
    except Exception as exc:  # the message may hold a path, so only the type is shown
        print(f"anbud answers {args.action}: refused: {type(exc).__name__}", file=sys.stderr)
        return REFUSED
    kinds = ", ".join(f"{kind} {n}" for kind, n in state.kinds.items()) or "none"
    print(f"answers: {state.split}, {'frozen' if state.frozen else 'not frozen'}; {state.total} files; kinds: {kinds}")
    return 0


def _not_built(name: str) -> int:
    print(f"anbud {name}: not built yet", file=sys.stderr)
    return NOT_BUILT


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # Subcommands with their own parsers.
    if argv and argv[0] == "guards":
        from anbud.guards.__main__ import main as guards_main

        return guards_main(argv[1:])
    if argv and argv[0] == "recipe-extract":
        from anbud.granskning.recipe_command import main as recipe_main

        return recipe_main(argv[1:])
    if argv and argv[0] == "split":
        from anbud.evals.split_heldout import main as split_main

        return split_main(argv[1:])
    if argv and argv[0] == "split-status":
        from anbud.evals.split_heldout import status_main

        return status_main(argv[1:])
    if argv and argv[0] == "schema-check":
        from anbud.schema_check import main as schema_check_main

        return schema_check_main(argv[1:])
    if argv and argv[0] == "plant":
        from anbud.evals.planted.cli import plant_main

        return plant_main(argv[1:])
    if argv and argv[0] == "plant-score":
        from anbud.evals.planted.cli import plant_score_main

        return plant_score_main(argv[1:])

    parser = argparse.ArgumentParser(prog="anbud", description="Estim Anbud harness")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run Granskning on a dev tender or a synthetic fixture")
    run.add_argument("company_id")
    run.add_argument("tender_id", nargs="?")
    run.add_argument("--fixture", type=Path, help="a fixture directory under fixtures/synthetic/")
    from anbud.granskning.command import add_arguments as add_run_arguments

    add_run_arguments(run)
    ev = sub.add_parser("eval", help="evals (dev now; held-out and prospective from phase 2, never from an agent)")
    ev.add_argument("kind", choices=["dev", "heldout", "prospective"])
    prep = sub.add_parser("prep", help="document preparation: blocks, outline, tiles and the masking report")
    prep.add_argument("company_id")
    prep.add_argument("tender_id", nargs="?")
    prep.add_argument("--fixture", type=Path, help="a fixture directory under fixtures/synthetic/")
    prep.add_argument(
        "--cells-only",
        action="store_true",
        help="add prep/cells.jsonl to an existing prep without changing anything else (only if the blocks match)",
    )
    kalkyl = sub.add_parser("kalkyl", help="Kalkyl in shadow: a dev tender's blocks to a priced calc XLSX (paid)")
    kalkyl.add_argument("company_id")
    kalkyl.add_argument("tender_id")
    kalkyl.add_argument("--catalog", help="catalog name in ~/estim-anbud-data/catalogs/ (default: config/kalkyl.toml)")
    kalkyl.add_argument(
        "--run-id",
        help="resume or replay an unfinished run (same salt, so finished calls cost $0); a finished run is frozen",
    )
    kalkyl.add_argument("--dry-run", action="store_true", help="chunk counts and a cost estimate, no model call ($0)")
    kalkyl.add_argument("--replay", action="store_true", help="answer every call from the run's step cache ($0)")
    kalkyl.add_argument(
        "--base-run",
        help="rebuild from this stored run: a new run with its salt, so unchanged requests cost $0; never writes to it",
    )
    sub.add_parser("replay", help="replay a stored run at $0 (phase 1)")
    sub.add_parser("guards", help="repo guards: names, document files, licensed text")
    sub.add_parser("split", help="assign new tenders to dev, held-out or prospective")
    sub.add_parser("recipe-extract", help="write the customer's outlet recipe from a run's calc recipe (explicit only)")
    sub.add_parser("split-status", help="counts per status, and the dev tender ids")
    sub.add_parser("schema-check", help="the shared tool list against strict-mode limits; --compile proves it (paid)")
    intake = sub.add_parser("intake", help="split new tenders from an incoming folder, then move them unopened")
    intake.add_argument("company_id")
    intake.add_argument("incoming", type=Path, help="~/estim-anbud-data/incoming/<company>/ with metadata.csv")
    intake.add_argument("--new-ledger", action="store_true", help="the company's first intake")
    intake.add_argument("--company-name", help="the company's name in the local registry, on its first intake")
    intake.add_argument(
        "--dry-run", action="store_true", help="zips only: what the notices give, counted; nothing written or split"
    )
    sub.add_parser("plant", help="a planted variant of a dev tender: plants, both control arms and the answer key")
    sub.add_parser("plant-score", help="score runs on a planted variant's arms against its answer key")
    bl = sub.add_parser("baseline", help="the app's current engine on a dev tender, frozen (paid unless --dry-run)")
    bl.add_argument("company_id")
    bl.add_argument("tender_id")
    bl.add_argument("--dry-run", action="store_true", help="run up to the first model call, which is never sent ($0)")
    bl.add_argument(
        "--hard-stop", type=float, help="lower the hard stop in config/budgets.toml [baseline]; never raise"
    )
    bl.add_argument("--app", type=Path, help="the app checkout (default: ~/Documents/estim/workspace/estim)")
    bl.add_argument("--app-ref", help="the app commit to export and run (default: origin/main)")
    cards = sub.add_parser("cards", help="case cards: counts per dev tender, sealed tenders counted only (local)")
    cards.add_argument("company_id")
    stop = sub.add_parser("stop", help="stop a run before its next model call, or every run with --all")
    stop.add_argument("company_id", nargs="?")
    stop.add_argument("run_id", nargs="?")
    stop.add_argument("--all", action="store_true", help="every run, in every company")
    stop.add_argument("--clear", action="store_true", help="remove the marker, so the run can resume")
    export = sub.add_parser("export-review", help="a blind XLSX of a run's flags, with decoys, for a human labeller")
    export.add_argument("company_id")
    export.add_argument("tender_id", nargs="?")
    export.add_argument("--fixture", type=Path, help="a fixture directory under fixtures/synthetic/")
    export.add_argument("--flags", type=Path, required=True, help="JSONL of review flags from the run's report")
    export.add_argument("--export-id", help="8 hex characters; random if left out")
    labels = sub.add_parser("import-labels", help="read a filled review XLSX back as labels")
    labels.add_argument("company_id")
    labels.add_argument("tender_id", nargs="?")
    labels.add_argument("--fixture", type=Path, help="a fixture directory under fixtures/synthetic/")
    labels.add_argument("--xlsx", type=Path, required=True, help="the filled review workbook")
    labels.add_argument("--labeller", required=True, help="a neutral labeller id, like lab-1")
    regress = sub.add_parser("regress", help="check a run against the tender's regression set of confirmed catches")
    regress.add_argument("company_id")
    regress.add_argument("tender_id")
    regress.add_argument("run_id")
    record = sub.add_parser("export-recording", help="copy a synthetic fixture's run into the repo for $0 replay")
    record.add_argument("company_id")
    record.add_argument("--fixture", type=Path, required=True, help="a fixture directory under fixtures/synthetic/")
    record.add_argument("--run-id", required=True)

    fr = sub.add_parser("freeze", help="the freeze ledger: hash a run before any answer for its tender arrives")
    fr_sub = fr.add_subparsers(dest="action", required=True)
    fr_run = fr_sub.add_parser("run", help="freeze a finished run")
    fr_run.add_argument("company_id")
    fr_run.add_argument("run_id")
    fr_run.add_argument("--tender", help="the run's tender (default: the one dev tender with this run id)")
    fr_run.add_argument("--outputs", type=Path, help="the run's outputs folder, inside the company folder")
    fr_verify = fr_sub.add_parser("verify", help="check the ledger's chain; counts only")
    fr_verify.add_argument("company_id")
    fr_import = fr_sub.add_parser("import", help="start the ledger with hand-made, chained freeze records")
    fr_import.add_argument("company_id")
    fr_import.add_argument("file", type=Path)
    an = sub.add_parser("answers", help="the answer store: only for tenders with a frozen prediction")
    an_sub = an.add_subparsers(dest="action", required=True)
    an_add = an_sub.add_parser("add", help="store answer files by hash")
    an_add.add_argument("company_id")
    an_add.add_argument("tender_id")
    an_add.add_argument("files", nargs="+", type=Path)
    an_add.add_argument("--kind", required=True, choices=["award", "protocol", "qa", "amendment", "feedback", "other"])
    an_status = an_sub.add_parser("status", help="answer counts by kind, and whether the tender is frozen")
    an_status.add_argument("company_id")
    an_status.add_argument("tender_id")

    args = parser.parse_args(argv)
    if args.command == "freeze":
        return _freeze(args)
    if args.command == "answers":
        return _answers(args)
    if args.command == "run":
        return _run(args)
    if args.command == "eval":
        return _eval(args)
    if args.command == "stop":
        return _stop(args)
    if args.command == "baseline":
        return _baseline(args)
    if args.command == "cards":
        return _cards(args)
    if args.command == "prep":
        return _prep(args)
    if args.command == "export-review":
        return _export_review(args)
    if args.command == "import-labels":
        return _import_labels(args)
    if args.command == "regress":
        return _regress(args)
    if args.command == "export-recording":
        return _export_recording(args)
    if args.command == "intake":
        return _intake(args)
    if args.command == "kalkyl":
        return _kalkyl(args)
    return _not_built(args.command)


if __name__ == "__main__":
    sys.exit(main())
