"""The `anbud` command."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from anbud.runguard import RunRefused, check_run_allowed, held_out_refusal

NOT_BUILT = 2
REFUSED = 3


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
    from anbud.prep.tender import PrepError, prepare
    from anbud.store import StoreError

    try:
        kind = check_run_allowed(args.company_id, args.tender_id, args.fixture)
    except RunRefused as exc:
        print(f"anbud prep: refused: {exc}", file=sys.stderr)
        return REFUSED
    try:
        if kind == "fixture":
            inbox = args.fixture.expanduser().resolve()
            out = paths.company_dir(args.company_id) / "fixtures" / inbox.name / "prep"
            manifest = None
        else:
            tender = paths.dev_tender_dir(args.company_id, args.tender_id)
            inbox, out, manifest = tender / "inbox", tender / "prep", tender / "inbox.sha256"
            if not inbox.resolve().is_relative_to(tender.resolve()):
                raise PrepError("the inbox resolves outside the tender's folder")
        summary = prepare(args.company_id, inbox, out, manifest)
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
            result, still_open = survey(args.incoming.expanduser())
            print(f"anbud intake (dry run, nothing written or split): {result.notices.tenders} zips")
            for line in intake_report(result, training_cutoff())[2:]:
                print(line)
            print(f"  deadlines still open: {still_open}")
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
        ]
        held = ", ".join(f"{k} {v}" for k, v in sorted(result.held.items())) or "none"
        lines.append(
            f"  held as answer keys: {sum(result.held.values())} files in {result.tenders_with_held} tenders ({held})"
        )
    return lines


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


def _not_built(name: str) -> int:
    print(f"anbud {name}: not built yet", file=sys.stderr)
    return NOT_BUILT


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # Subcommands with their own parsers.
    if argv and argv[0] == "guards":
        from anbud.guards.__main__ import main as guards_main

        return guards_main(argv[1:])
    if argv and argv[0] == "split":
        from anbud.evals.split_heldout import main as split_main

        return split_main(argv[1:])
    if argv and argv[0] == "split-status":
        from anbud.evals.split_heldout import status_main

        return status_main(argv[1:])
    if argv and argv[0] == "schema-check":
        from anbud.schema_check import main as schema_check_main

        return schema_check_main(argv[1:])

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
    sub.add_parser("replay", help="replay a stored run at $0 (phase 1)")
    sub.add_parser("guards", help="repo guards: names, document files, licensed text")
    sub.add_parser("split", help="assign new tenders to dev, held-out or prospective")
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
    stop = sub.add_parser("stop", help="stop a run before its next model call, or every run with --all")
    stop.add_argument("company_id", nargs="?")
    stop.add_argument("run_id", nargs="?")
    stop.add_argument("--all", action="store_true", help="every run, in every company")
    stop.add_argument("--clear", action="store_true", help="remove the marker, so the run can resume")

    args = parser.parse_args(argv)
    if args.command == "run":
        return _run(args)
    if args.command == "eval":
        return _eval(args)
    if args.command == "stop":
        return _stop(args)
    if args.command == "prep":
        return _prep(args)
    if args.command == "intake":
        return _intake(args)
    return _not_built(args.command)


if __name__ == "__main__":
    sys.exit(main())
