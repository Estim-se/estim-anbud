"""Recordings of paid runs on synthetic fixtures, committed so CI replays them at $0 (plan §8.6).

`anbud export-recording <company> --fixture <dir> --run-id <id>` copies one run of a fixture under
`fixtures/synthetic/` into `<fixture's parent>/recordings/<fixture name>/`:

    recording.json   the run id, company id and fixture path
    run.json         the run's manifest: its salt and the code it ran with
    outputs/         items.json, flags.json and kronor.json from the run folder, and the run's granskning.json
    store/steps/     the step-cache entries the run's calls used, without the API's request ids
    store/blobs/     their request and response bodies, and the map, lens and summary blobs the run logged

`install_recording` puts a recording back into a data root, so `anbud run --fixture <dir> --run-id <id> --replay`
answers every call from it.

Only fixtures are exported, never a customer tender. The run must have used no catalog: catalog prices never go into
the repo, and a replay without the same catalog would rank, verify and price differently. Record in a data root of its
own (`ANBUD_DATA_ROOT`) that holds no `catalogs/` file.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from anbud import paths
from anbud.granskning.catalog import DEFAULT_CATALOG, catalog_path
from anbud.store import RunStore, canonical

OUTPUTS = ("items.json", "flags.json", "kronor.json")


class RecordingError(RuntimeError):
    """Raised with a reason that names no tender content."""


def recording_dir(fixture: Path) -> Path:
    fixture = fixture.expanduser().resolve()
    return fixture.parent / "recordings" / fixture.name


def _is_text(data: bytes) -> bool:
    try:
        json.loads(data)
    except ValueError:  # tiles and other binary blobs aren't JSON; prep makes them again
        return False
    return True


def export_recording(
    store: RunStore, fixture: Path, run_id: str, root: Path | None = None, dest: Path | None = None
) -> dict[str, Any]:
    fixture = fixture.expanduser().resolve()
    synthetic = (paths.repo_root() / "fixtures" / "synthetic").resolve()
    if not fixture.is_relative_to(synthetic):
        raise RecordingError("only fixtures under fixtures/synthetic/ are recorded")
    if catalog_path(DEFAULT_CATALOG, root).exists():
        raise RecordingError("this data root holds a catalog; record in a data root without one")
    run_dir = paths.fixture_data_dir(store.company_id, fixture, root) / "runs" / run_id
    summary = store.run_dir(run_id) / "granskning.json"
    manifest = store.run_dir(run_id) / "run.json"
    missing = [p.name for p in (*(run_dir / n for n in OUTPUTS), summary, manifest) if not p.exists()]
    if missing:
        raise RecordingError(f"the run lacks {', '.join(missing)}")

    logged: set[str] = set()
    for event in store.events(run_id):
        logged |= {b for b in (event.blob, event.request_blob) if b}
    steps: dict[Path, dict[str, Any]] = {}
    for path in sorted((store.dir / "steps").glob("*/*.json")):
        record = json.loads(path.read_text())
        if record.get("request") in logged or record.get("response") in logged:
            steps[path] = record
    if not steps:
        raise RecordingError("no step-cache entries belong to this run")
    blobs = logged | {b for r in steps.values() for b in (r["request"], r["response"])}

    dest = dest or recording_dir(fixture)
    if dest.exists():
        shutil.rmtree(dest)
    for path, record in steps.items():  # the API's request ids stay local; replay doesn't need them
        target = dest / "store" / "steps" / path.parent.name / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(canonical({k: v for k, v in record.items() if k != "request_id"}))
    copied = 0
    for sha in sorted(blobs):
        data = store.get_blob(sha)
        if not _is_text(data):
            continue
        target = dest / "store" / "blobs" / sha[:2] / sha
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        copied += 1
    (dest / "outputs").mkdir(parents=True)
    for name in OUTPUTS:
        shutil.copyfile(run_dir / name, dest / "outputs" / name)
    shutil.copyfile(summary, dest / "outputs" / "granskning.json")
    shutil.copyfile(manifest, dest / "run.json")
    fixture_rel = fixture.relative_to(synthetic).as_posix()
    info = {"run_id": run_id, "company_id": store.company_id, "fixture": fixture_rel}
    (dest / "recording.json").write_text(json.dumps(info, indent=1) + "\n")
    return {"recording": str(dest), "steps": len(steps), "blobs": copied}


def install_recording(src: Path, root: Path | None = None) -> dict[str, Any]:
    """Copy a recording's step cache, blobs and run manifest into a data root. Returns recording.json."""
    info = json.loads((src / "recording.json").read_text())
    store = RunStore(info["company_id"], root)
    for kind in ("steps", "blobs"):
        for path in sorted((src / "store" / kind).glob("*/*")):
            store.write_file(store.dir / kind / path.parent.name / path.name, path.read_bytes())
    store.write_file(store.run_dir(info["run_id"]) / "run.json", (src / "run.json").read_bytes())
    return info
