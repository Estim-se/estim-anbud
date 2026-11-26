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
    print(f"anbud run: allowed ({kind}), but the pipeline isn't built yet (phase 1)", file=sys.stderr)
    return NOT_BUILT


def _eval(args: argparse.Namespace) -> int:
    if args.kind in ("heldout", "prospective"):
        refusal = held_out_refusal()
        if refusal:
            print(f"anbud eval {args.kind}: refused: {refusal}", file=sys.stderr)
            return REFUSED
    print(f"anbud eval {args.kind}: not built yet", file=sys.stderr)
    return NOT_BUILT


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

    parser = argparse.ArgumentParser(prog="anbud", description="Estim Anbud harness")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run Granskning on a dev tender or a synthetic fixture")
    run.add_argument("company_id")
    run.add_argument("tender_id", nargs="?")
    run.add_argument("--fixture", type=Path, help="a fixture directory under fixtures/synthetic/")
    ev = sub.add_parser("eval", help="evals (dev now; held-out and prospective from phase 2, never from an agent)")
    ev.add_argument("kind", choices=["dev", "heldout", "prospective"])
    sub.add_parser("prep", help="document preparation (phase 1)")
    sub.add_parser("replay", help="replay a stored run at $0 (phase 1)")
    sub.add_parser("guards", help="repo guards: names, document files, licensed text")
    sub.add_parser("split", help="assign new tenders to dev, held-out or prospective")
    sub.add_parser("split-status", help="counts per status, and the dev tender ids")

    args = parser.parse_args(argv)
    if args.command == "run":
        return _run(args)
    if args.command == "eval":
        return _eval(args)
    return _not_built(args.command)


if __name__ == "__main__":
    sys.exit(main())
