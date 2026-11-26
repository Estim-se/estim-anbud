"""`anbud plant` and `anbud plant-score`. Both print counts, ids and kinds only, never tender or calc text.

    anbud plant <company> <tender> --seed N   a planted variant of a dev tender, in its data folder
    anbud plant-score <run> [<run> ...]       score runs on one variant's arms against its answer key

Two seams wait for the stages being built alongside:
- `load_tender_calc` reads the tender's calc as typed posts from `<tender>/calc/posts.jsonl` until the calc ingest
  replaces it;
- `load_run` reads a run's `inputs.json` (RunInputs; its `prep_dir` names the arm), `flags.json` (a list of Flag) and,
  if present, `kronor.json` (kronor by flag fingerprint).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from anbud import paths
from anbud.evals.planted.key import ARMS, Arm, Key
from anbud.evals.planted.score import RunFlags, score
from anbud.evals.planted.structural import CatalogItem
from anbud.evals.planted.variant import build_variant, variant_dir
from anbud.granskning.contracts import CalcPost, RunInputs
from anbud.models import Flag
from anbud.runguard import RunRefused, check_run_allowed
from anbud.store import RunStore, StoreError, check_run_id

REFUSED = 3


class ScoreError(RuntimeError):
    pass


def load_tender_calc(tender_dir: Path) -> list[CalcPost]:
    path = tender_dir / "calc" / "posts.jsonl"
    if not path.exists():
        raise StoreError("the tender has no typed calc yet (calc/posts.jsonl); ingest its calc first")
    return [CalcPost.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()]


def _catalog(path: Path | None) -> dict[str, CatalogItem] | None:
    if path is None:
        return None
    raw = json.loads(path.read_text())
    return {k: CatalogItem(v.get("unit_price"), v.get("hours_per_unit")) for k, v in raw.items()}


def plant_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="anbud plant", description="a planted variant of a dev tender")
    parser.add_argument("company_id")
    parser.add_argument("tender_id")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--calc-plants", type=int, default=3)
    parser.add_argument("--doc-plants", type=int, default=2)
    parser.add_argument("--catalog", type=Path, help="catalog JSON for the structural detector's catalog diffs")
    parser.add_argument("--force", action="store_true", help="write the variant again if it exists")
    args = parser.parse_args(argv)
    try:
        kind = check_run_allowed(args.company_id, args.tender_id)
    except RunRefused as exc:
        print(f"anbud plant: refused: {exc}", file=sys.stderr)
        return REFUSED
    if kind != "dev" or args.seed < 0:
        print("anbud plant: refused: dev tenders only, with a seed of 0 or more", file=sys.stderr)
        return REFUSED
    tender = paths.dev_tender_dir(args.company_id, args.tender_id)
    prep = tender / "prep"
    if not (prep / "blocks.jsonl").exists():
        print("anbud plant: stopped: the tender isn't prepared; run `anbud prep` first", file=sys.stderr)
        return REFUSED
    out = variant_dir(tender, args.seed)
    try:
        posts = load_tender_calc(tender)
        key = build_variant(
            args.company_id,
            args.tender_id,
            posts,
            prep,
            out,
            args.seed,
            calc_plants=args.calc_plants,
            doc_plants=args.doc_plants,
            catalog=_catalog(args.catalog),
            force=args.force,
        )
    except (StoreError, ValidationError, OSError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, StoreError) else type(exc).__name__
        print(f"anbud plant: stopped: {reason}", file=sys.stderr)
        return REFUSED
    print(f"variant: {out}")
    print(f"plants: {len(key.plants)}")
    for kind_name, n in sorted(Counter(p.kind for p in key.plants).items()):
        print(f"  {kind_name}: {n}")
    print(f"structural findings (planted / unplanted): {len(key.structural)} / {len(key.structural_unplanted)}")
    print("arms: " + ", ".join(ARMS) + "; the answer key is in key/ and no run may read it")
    return 0


def find_run(arg: str, root: Path | None = None) -> Path:
    """A run folder: a path to one, or a run id looked up across the companies' run stores."""
    base = (root or paths.data_root()).expanduser()
    candidate = Path(arg).expanduser()
    if candidate.is_dir():
        run_dir = candidate.resolve()
    else:
        check_run_id(arg)
        hits = sorted(p for p in base.glob(f"co-*/runs/{arg}") if p.is_dir())
        if not hits:
            raise ScoreError("no run with that id in any company's run store")
        if len(hits) > 1:
            raise ScoreError("that run id exists in more than one company; give the run's folder")
        run_dir = hits[0].resolve()
    if paths.is_sealed_path(run_dir, root):
        raise ScoreError("never scores held-out or sealed runs")
    return run_dir


def load_run(run_dir: Path) -> tuple[RunInputs, RunFlags]:
    try:
        inputs = RunInputs.model_validate_json((run_dir / "inputs.json").read_text())
        flags = TypeAdapter(list[Flag]).validate_json((run_dir / "flags.json").read_bytes())
        kronor_path = run_dir / "kronor.json"
        kronor = TypeAdapter(dict[str, float]).validate_json(kronor_path.read_bytes()) if kronor_path.exists() else {}
    except FileNotFoundError:
        raise ScoreError("a run needs inputs.json and flags.json to be scored") from None
    except ValidationError:
        raise ScoreError("a run's inputs.json, flags.json or kronor.json doesn't validate") from None
    return inputs, RunFlags(run_id=run_dir.name, flags=flags, calc_posts=inputs.calc_posts, kronor=kronor)


def arm_of(prep_dir: str) -> tuple[Path, Arm]:
    """The variant folder and the arm a run read, from its prep folder: `<variant>/arms/<arm>/prep`."""
    prep = Path(prep_dir).expanduser().resolve()
    arm = prep.parent.name
    if prep.name != "prep" or prep.parent.parent.name != "arms" or arm not in ARMS:
        raise ScoreError("the run didn't read a planted variant's arm")
    return prep.parent.parent.parent, arm  # type: ignore[return-value]


def score_runs(run_dirs: list[Path], key_path: Path | None = None) -> tuple[Key, dict, Path]:
    variant = None
    runs: dict[Arm, RunFlags] = {}
    company = None
    for run_dir in run_dirs:
        inputs, flags = load_run(run_dir)
        v, arm = arm_of(inputs.prep_dir)
        if variant is not None and v != variant:
            raise ScoreError("the runs read different variants")
        if arm in runs:
            raise ScoreError(f"two runs on the {arm} arm; give one per arm")
        if company is not None and inputs.company_id != company:
            raise ScoreError("the runs belong to different companies")
        variant, company, runs[arm] = v, inputs.company_id, flags
    if "planted" not in runs or variant is None:
        raise ScoreError("one of the runs must be on the planted arm")
    key_file = key_path or variant / "key" / "plants.json"
    key = Key.model_validate_json(key_file.read_text())
    if key.company_id != company:
        raise ScoreError("the answer key belongs to another company")
    report = score(key, runs)
    return key, report.model_dump(), variant


def plant_score_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="anbud plant-score", description="score runs on a planted variant")
    parser.add_argument("runs", nargs="+", help="run ids or run folders: the planted arm, plus any control arms")
    parser.add_argument("--key", type=Path, help="the answer key, if not the variant's own key/plants.json")
    args = parser.parse_args(argv)
    try:
        run_dirs = [find_run(r) for r in args.runs]
        key, report, variant = score_runs(run_dirs, args.key)
        out = variant / "key" / "scores" / f"{report['runs']['planted']}.json"
        RunStore(key.company_id).write_file(out, json.dumps(report, indent=2, sort_keys=True).encode())
    except (ScoreError, StoreError, OSError, ValidationError) as exc:
        reason = str(exc) if isinstance(exc, ScoreError | StoreError) else type(exc).__name__
        print(f"anbud plant-score: stopped: {reason}", file=sys.stderr)
        return REFUSED
    print(f"report: {out}")
    print(f"arms scored: {', '.join(sorted(report['runs']))}; complete: {report['complete']}")
    for name, r in report["recall_clean"].items():
        if r["plants"]:
            counts = f"{r['caught']}/{r['plants']}, unresolved {r['unresolved']}"
            print(f"recall (both controls miss) {name}: {counts}, kronor-weighted {r['kronor_recall']}")
    for name, value in report["controls"].items():
        print(f"control {name}: {value}")
    for name, value in report["baseline"].items():
        print(f"unplanted baseline {name}: {value}")
    return 0
