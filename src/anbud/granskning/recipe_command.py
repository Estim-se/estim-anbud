"""`anbud recipe-extract <company> <tender> [--run-id ID] [--force]`: write the customer's outlet recipe from the calc
recipe a run of that dev tender computed.

The recipe comes from code only: the posts a run's outlet check mapped to room types, counted per room from the
calc. No model call. It is written to `<data>/<company>/references/outlet-recipe.toml` with
`source = "extracted from customer calc <tender>"`, and an existing recipe is kept unless `--force` is given. Only a
person decides when this runs. Prints counts and the path, never calc text.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from anbud import paths
from anbud.granskning.outlet_recipe import RecipeSection, extract_recipe, recipe_path, recipe_toml
from anbud.runguard import RunRefused, check_run_allowed
from anbud.store import RunStore, StoreError

REFUSED = 3


def _checks_file(runs: Path, run_id: str | None) -> Path | None:
    if run_id is not None:
        path = runs / run_id / "checks.json"
        return path if path.exists() else None
    found = sorted(runs.glob("*/checks.json"), key=lambda p: p.stat().st_mtime) if runs.is_dir() else []
    return found[-1] if found else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="anbud recipe-extract", description=__doc__.splitlines()[0])
    parser.add_argument("company_id")
    parser.add_argument("tender_id")
    parser.add_argument("--run-id", help="the run whose calc recipe to use (default: the latest with checks)")
    parser.add_argument("--force", action="store_true", help="replace an existing customer recipe")
    args = parser.parse_args(argv)
    try:
        if check_run_allowed(args.company_id, args.tender_id) != "dev":
            raise RunRefused("only dev tenders")
    except RunRefused as exc:
        print(f"anbud recipe-extract: refused: {exc}", file=sys.stderr)
        return REFUSED
    runs = paths.dev_tender_dir(args.company_id, args.tender_id) / "runs"
    source = _checks_file(runs, args.run_id)
    if source is None:
        print("anbud recipe-extract: no run of this tender has the outlet check's results", file=sys.stderr)
        return REFUSED
    section = RecipeSection.model_validate(json.loads(source.read_text())["recipe"] or {"status": "failed"})
    rooms = extract_recipe(section)
    if not rooms:
        print("anbud recipe-extract: the run mapped no calc posts to room types; nothing to write", file=sys.stderr)
        return REFUSED
    target = recipe_path(args.company_id)
    if target.exists() and not args.force:
        print(f"anbud recipe-extract: {target} exists; pass --force to replace it", file=sys.stderr)
        return REFUSED
    text = recipe_toml(rooms, f"extracted from customer calc {args.tender_id}")
    try:
        RunStore(args.company_id).write_file(target, text.encode())
    except (OSError, StoreError) as exc:
        print(f"anbud recipe-extract: can't write the recipe ({type(exc).__name__})", file=sys.stderr)
        return REFUSED
    kinds = {k for v in rooms.values() for k in v}
    print(f"recipe: {len(rooms)} room types, {len(kinds)} item kinds, from run {source.parent.name}")
    print(f"written: {target}")
    return 0
