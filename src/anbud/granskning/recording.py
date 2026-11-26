"""Recordings of paid runs on synthetic fixtures, committed so CI replays them at $0 (plan §8.6).

`anbud export-recording <company> --fixture <dir> --run-id <id>` copies one run of a fixture under
`fixtures/synthetic/` into `<fixture's parent>/recordings/<fixture name>/`:

    recording.json   the run id, company id, fixture path and the lens set the run used
    run.json         the run's manifest: its salt and the code it ran with
    outputs/         items.json, flags.json and kronor.json from the run folder, and the run's granskning.json
    store/steps/     the step-cache entries the run's calls used, without the API's request ids
    store/blobs/     their request and response bodies, and the map, lens and summary blobs the run logged
    prep/tiles.json  the tiles the page cards read, and tiles/<sha256>.png, their images

`install_recording` puts a recording back into a data root and `use_recorded_tiles` points a fresh prep at the recorded
tiles, so a replay never depends on how this machine renders PNGs: the page-card request carries the tile's bytes.
Then `anbud run --fixture <dir> --run-id <id> --replay` with `replay_arguments()` answers every call from the
recording. The lens set is part of the map's request, so a replay with another set misses the cache and exits 4.

If the fixture's folder holds an `expected.json` with a `lenses` list, the run must have used exactly that set. A
recording written into the repo adds its tile images to `fixtures/synthetic/binary-manifest.sha256`: they are renders
of the reviewed drawing, but look at them before committing.

Only fixtures are exported, never a customer tender. The run must have used no catalog: catalog prices never go into
the repo, and a replay without the same catalog would rank, verify and price differently. Record in a data root of its
own (`ANBUD_DATA_ROOT`) that holds no `catalogs/` file.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from anbud import paths
from anbud.granskning.catalog import DEFAULT_CATALOG, catalog_path
from anbud.store import RunStore, canonical

OUTPUTS = ("items.json", "flags.json", "kronor.json")
PNG = b"\x89PNG\r\n\x1a\n"


class RecordingError(RuntimeError):
    """Raised with a reason that names no tender content."""


def recording_dir(fixture: Path) -> Path:
    fixture = fixture.expanduser().resolve()
    return fixture.parent / "recordings" / fixture.name


def _is_text(data: bytes) -> bool:
    try:
        json.loads(data)
    except ValueError:  # tiles go into tiles/ as PNG files
        return False
    return True


def lens_set(summary: dict[str, Any]) -> list[str]:
    return sorted(entry["lens_id"] for entry in summary["lenses"])


def _check_lenses(fixture: Path, lenses: list[str]) -> None:
    expected = fixture.parent / "expected.json"
    if not expected.exists():
        return
    wanted = json.loads(expected.read_text()).get("lenses")
    if wanted and sorted(wanted) != lenses:
        flags = " ".join(f"--lens {lens}" for lens in sorted(wanted))
        raise RecordingError(f"the run used lenses {lenses}; record with {flags}")


def _update_manifest(dest: Path, tiles: list[Path]) -> None:
    """Replace this recording's lines in the binary-fixture manifest with its current tile images."""
    repo = paths.repo_root().resolve()
    manifest = repo / "fixtures" / "synthetic" / "binary-manifest.sha256"
    prefix = dest.resolve().relative_to(repo).as_posix() + "/"
    lines = manifest.read_text().splitlines() if manifest.exists() else []
    kept = [ln for ln in lines if not (ln.split(maxsplit=1)[1:] or [""])[0].startswith(prefix)]
    added = [f"{hashlib.sha256(t.read_bytes()).hexdigest()}  {t.resolve().relative_to(repo).as_posix()}" for t in tiles]
    manifest.write_text("\n".join(kept + added) + "\n")


def export_recording(
    store: RunStore, fixture: Path, run_id: str, root: Path | None = None, dest: Path | None = None
) -> dict[str, Any]:
    fixture = fixture.expanduser().resolve()
    synthetic = (paths.repo_root() / "fixtures" / "synthetic").resolve()
    if not fixture.is_relative_to(synthetic):
        raise RecordingError("only fixtures under fixtures/synthetic/ are recorded")
    if catalog_path(DEFAULT_CATALOG, root).exists():
        raise RecordingError("this data root holds a catalog; record in a data root without one")
    data_dir = paths.fixture_data_dir(store.company_id, fixture, root)
    run_dir = data_dir / "runs" / run_id
    summary = store.run_dir(run_id) / "granskning.json"
    manifest = store.run_dir(run_id) / "run.json"
    tiles_json = data_dir / "prep" / "tiles.json"
    needed = (*(run_dir / n for n in OUTPUTS), summary, manifest, tiles_json)
    missing = [p.name for p in needed if not p.exists()]
    if missing:
        raise RecordingError(f"the run lacks {', '.join(missing)}")
    lenses = lens_set(json.loads(summary.read_text()))
    _check_lenses(fixture, lenses)

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
    tiles = []
    (dest / "tiles").mkdir(parents=True)
    for tile in json.loads(tiles_json.read_text()):
        data = store.get_blob(tile["blob"])
        if not data.startswith(PNG):
            raise RecordingError("a tile blob isn't a PNG")
        tiles.append(dest / "tiles" / f"{tile['blob']}.png")
        tiles[-1].write_bytes(data)
    (dest / "prep").mkdir()
    shutil.copyfile(tiles_json, dest / "prep" / "tiles.json")
    (dest / "outputs").mkdir(parents=True)
    for name in OUTPUTS:
        shutil.copyfile(run_dir / name, dest / "outputs" / name)
    shutil.copyfile(summary, dest / "outputs" / "granskning.json")
    shutil.copyfile(manifest, dest / "run.json")
    fixture_rel = fixture.relative_to(synthetic).as_posix()
    info = {"run_id": run_id, "company_id": store.company_id, "fixture": fixture_rel, "lenses": lenses}
    (dest / "recording.json").write_text(json.dumps(info, indent=1) + "\n")
    if dest.resolve().is_relative_to(synthetic):
        _update_manifest(dest, tiles)
    return {"recording": str(dest), "steps": len(steps), "blobs": copied, "tiles": len(tiles), "lenses": lenses}


def install_recording(src: Path, root: Path | None = None) -> dict[str, Any]:
    """Copy a recording's step cache, blobs, tile images and run manifest into a data root. Returns recording.json."""
    info = json.loads((src / "recording.json").read_text())
    store = RunStore(info["company_id"], root)
    for kind in ("steps", "blobs"):
        for path in sorted((src / "store" / kind).glob("*/*")):
            store.write_file(store.dir / kind / path.parent.name / path.name, path.read_bytes())
    for path in sorted((src / "tiles").glob("*.png")):
        if store.put_blob(path.read_bytes()) != path.stem:
            raise RecordingError("a recorded tile doesn't match its name")
    store.write_file(store.run_dir(info["run_id"]) / "run.json", (src / "run.json").read_bytes())
    return info


def use_recorded_tiles(src: Path, prep_dir: Path, root: Path | None = None) -> None:
    """Point a fresh prep of the fixture at the recorded tiles, instead of the ones this machine rendered."""
    info = json.loads((src / "recording.json").read_text())
    RunStore(info["company_id"], root).write_file(prep_dir / "tiles.json", (src / "prep" / "tiles.json").read_bytes())


def replay_arguments(src: Path, lenses: list[str] | None = None) -> list[str]:
    """`--lens` flags for the recorded lens set. Given `lenses`, they must be that set, or it raises."""
    recorded = json.loads((src / "recording.json").read_text())["lenses"]
    if lenses is not None and sorted(lenses) != recorded:
        raise RecordingError(f"the recording used lenses {recorded}, not {sorted(lenses)}; a replay must use the same")
    return [arg for lens in recorded for arg in ("--lens", lens)]
