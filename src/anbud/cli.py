"""The `anbud` command."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

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
    from anbud.prep.tender import FIXTURE_EXCLUDE, PrepError, prepare
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

    args = parser.parse_args(argv)
    if args.command == "run":
        return _run(args)
    if args.command == "eval":
        return _eval(args)
    if args.command == "stop":
        return _stop(args)
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
    return _not_built(args.command)


if __name__ == "__main__":
    sys.exit(main())
