"""`anbud baseline <co> <t>`: the app's current engine on a dev tender, as a frozen baseline for the new engine.

The app's `scripts/run-anbud-local.ts` runs from a clean export of one app commit (default `origin/main`), never from
the app's working copy, so the baseline is that commit and nothing uncommitted. The export borrows the app checkout's
`node_modules`; no file named `.env*` and no `bunfig.toml` is extracted.

The engine runs under `bun --no-env-file --no-install` with a minimal environment: PATH, HOME, TMPDIR, the model pin
and the `anbud-dev` key, read from the Keychain into the child's environment only. `baseline_preload.mjs` wraps fetch
underneath the script: it refuses every host but the Messages API, logs each call's ids, usage and cost (never text)
and enforces the hard stop. Nothing here runs `verify-anbud-pipeline.ts` or touches Supabase.

Everything goes to `predictions/<co>/baseline/<t>/<run_id>/`, which only the eval lane and the orchestrator open,
never under `dev/` or `<co>/runs/`: the old engine's total is an anchor the build lane must not see. `outputs/` holds
engine.json (the script's --out), `record/` holds run.json, calls.jsonl and engine.log, and `totals.json` (written
after the freeze, outside both) holds the total and its labour, material and hours split. A finished paid run is
frozen. The CLI prints counts, costs, timings and ids only.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
import tomllib
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from anbud import paths
from anbud.config import CONFIG_DIR, load_config
from anbud.store import check_run_id, private_dirs

APP_DIR = Path.home() / "Documents/estim/workspace/estim"
APP_REF = "origin/main"
SCRIPT = "scripts/run-anbud-local.ts"
MODEL = "claude-sonnet-5"  # the script's forced tool_choice rules out 5.5
SUFFIXES = (".pdf", ".docx", ".xlsx")  # what the script reads; everything else is counted as skipped
PRELOAD = Path(__file__).resolve().with_name("baseline_preload.mjs")
SETTINGS_SCRIPT = Path(__file__).resolve().with_name("baseline_settings.mjs")
# "matched" prices on Kalkyl's basis: the default catalog's hourly rate, list prices as material with no discount or
# markup, and no establishment or contingency. "app-defaults" is the script's own example settings (the app today).
BASES = ("matched", "app-defaults")
ZERO_MARKUPS = {"material_markup": 0, "list_discount": 0, "establishment": 0, "contingency": 0}
EXIT_CODES = {0: "ok", 86: "hard_stop", 87: "blocked", 88: "dry_run"}
POLL_SECONDS = 2.0


class BaselineError(RuntimeError):
    """Raised with a reason that names no tender content."""


@dataclass
class Outcome:
    run_id: str
    outcome: str  # ok, dry_run, hard_stop, blocked, failed, stopped, timeout
    exit_code: int | None
    documents: int
    skipped_by_type: dict[str, int]
    calls: int = 0
    calls_failed: int = 0
    calls_incomplete: int = 0
    cost_usd: float = 0.0
    in_flight_usd_at_stop: float = 0.0
    duration_s: float = 0.0
    workspace_ids: list[str] = field(default_factory=list)
    organization_ids: list[str] = field(default_factory=list)
    env_names: list[str] = field(default_factory=list)
    blocked_hosts: int = 0
    calls_waited: int = 0  # calls that waited for room under the hard stop (the preload's `Room`)
    wait_ms_max: int = 0
    output_file: bool = False


def baseline_budget(config_dir: Path = CONFIG_DIR) -> dict[str, Any]:
    return dict(tomllib.loads((config_dir / "budgets.toml").read_text())["baseline"])


def baseline_price(config_dir: Path = CONFIG_DIR) -> dict[str, float]:
    return dict(tomllib.loads((config_dir / "prices.toml").read_text())[MODEL])


# --- The app export -----------------------------------------------------------------------------------------------


def _git(app: Path, *args: str) -> bytes:
    try:
        return subprocess.run(["git", "-C", str(app), *args], capture_output=True, check=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError):
        raise BaselineError(f"git {args[0]} failed in the app checkout") from None


def resolve_app(app: Path, ref: str) -> str:
    """The full commit sha of `ref` in the app checkout."""
    if not (app / ".git").exists():
        raise BaselineError("the app folder isn't a git checkout; pass --app")
    return _git(app, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()


def _skipped_member(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return any(p.startswith(".env") for p in parts) or parts[-1:] == ("bunfig.toml",)


def export_app(app: Path, sha: str, root: Path | None = None) -> Path:
    """`<data>/baseline/app/<sha>/`: the commit's files, without `.env*` or `bunfig.toml`, and a link to the app
    checkout's node_modules. Built once per commit."""
    target = (root or paths.data_root()) / "baseline" / "app" / sha
    if (target / SCRIPT).is_file():
        return target
    private_dirs(target.parent)
    staging = target.with_name(f".{sha}.tmp")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(mode=0o700)
    archive = _git(app, "archive", "--format=tar", sha)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        members = [m for m in tar.getmembers() if not _skipped_member(m.name)]
        tar.extractall(staging, members=members, filter="data")
    modules = app / "node_modules"
    if not modules.is_dir():
        shutil.rmtree(staging, ignore_errors=True)
        raise BaselineError("the app checkout has no node_modules; run `bun install` there first")
    (staging / "node_modules").symlink_to(modules.resolve(), target_is_directory=True)
    if not (staging / SCRIPT).is_file():
        shutil.rmtree(staging, ignore_errors=True)
        raise BaselineError(f"the app commit has no {SCRIPT}")
    staging.rename(target)
    return target


# --- Documents ----------------------------------------------------------------------------------------------------


def flat_documents(files: Iterable[Path], dest: Path) -> tuple[int, dict[str, int]]:
    """Copy the files the script reads into one flat folder (it reads no subfolders). Two files with one name get
    ` (2)`, ` (3)` before the suffix. Returns how many were copied, and the skipped ones by type."""
    private_dirs(dest)
    used: set[str] = set()
    skipped: Counter[str] = Counter()
    copied = 0
    for path in sorted(files):
        suffix = path.suffix.casefold()
        if suffix not in SUFFIXES:
            skipped[suffix.lstrip(".") or "none"] += 1
            continue
        name, n = path.name, 1
        while name.casefold() in used:
            n += 1
            name = f"{path.stem} ({n}){path.suffix}"
        used.add(name.casefold())
        shutil.copyfile(path, dest / name, follow_symlinks=False)
        os.chmod(dest / name, 0o600)
        copied += 1
    return copied, dict(skipped.most_common())


def dev_inbox_files(company_id: str, tender_id: str, root: Path | None = None) -> list[Path]:
    """The files in a dev tender's inbox manifest, each a real file inside the inbox."""
    tender = paths.dev_tender_dir(company_id, tender_id, root)
    manifest = tender / "inbox.sha256"
    inbox = (tender / "inbox").resolve()
    if not manifest.is_file() or manifest.is_symlink():
        raise BaselineError("the tender has no inbox manifest; run intake first")
    out = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if "  " not in line:
            continue
        path = tender / "inbox" / line.split("  ", 1)[1]
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(inbox):
            raise BaselineError("an inbox manifest entry isn't a plain file in the inbox")
        out.append(path)
    if not out:
        raise BaselineError("the inbox manifest lists no files")
    return out


# --- The engine ---------------------------------------------------------------------------------------------------


def engine_command(
    bun: str, app_root: Path, documents: Path, out_file: Path, settings: Path | None = None
) -> list[str]:
    return [
        bun,
        "--no-env-file",
        "--no-install",
        f"--tsconfig-override={app_root / 'tsconfig.json'}",
        f"--preload={PRELOAD}",
        str(app_root / SCRIPT),
        "--documents",
        str(documents),
        "--out",
        str(out_file),
        *(["--settings", str(settings)] if settings is not None else []),
    ]


def engine_env(
    bun: str, api_key: str, calls_log: Path, hard_stop: float, price: dict[str, float], dry_run: bool, tmp: Path
) -> dict[str, str]:
    """The child's whole environment. The key lives here and nowhere else."""
    return {
        "PATH": f"{Path(bun).parent}:/usr/bin:/bin",
        "HOME": str(Path.home()),
        "TMPDIR": str(tmp),
        "ANTHROPIC_API_KEY": api_key,
        "ANTHROPIC_MODEL_MAIN": MODEL,
        "ANBUD_BASELINE_CALLS": str(calls_log),
        "ANBUD_BASELINE_HARD_STOP": repr(float(hard_stop)),
        "ANBUD_BASELINE_PRICE": json.dumps(price),
        "ANBUD_BASELINE_DRY": "1" if dry_run else "0",
    }


def matched_basis(root: Path | None = None, config_dir: Path = CONFIG_DIR) -> dict[str, Any]:
    """Kalkyl's pricing basis: its default catalog's hourly rate and no markups. Returns the settings values and, for
    the run record, the catalog's name and hash."""
    name = tomllib.loads((config_dir / "kalkyl.toml").read_text())["catalog"]["default"]
    path = (root or paths.data_root()) / "catalogs" / f"{name}.json"
    if path.is_symlink() or not path.is_file():
        raise BaselineError("Kalkyl's default catalog file is missing; matched settings need its hourly rate")
    data = path.read_bytes()
    rate = json.loads(data).get("hourly_rate")
    if not isinstance(rate, int | float) or rate <= 0:
        raise BaselineError("Kalkyl's default catalog has no hourly rate; matched settings need one")
    return {
        "values": {"hourly_rate": rate, **ZERO_MARKUPS},
        "catalog": name,
        "catalog_sha256": hashlib.sha256(data).hexdigest(),
    }


def write_settings(bun: str, app_root: Path, values: dict[str, Any], out: Path, tmp: Path) -> str:
    """The old engine's settings on `values`, built by the app's own code from the exported commit, under the preload
    in dry-run mode (so no network). Returns the file's sha256. The app's messages are never shown."""
    env = {
        "PATH": f"{Path(bun).parent}:/usr/bin:/bin",
        "HOME": str(Path.home()),
        "TMPDIR": str(tmp),
        "ANBUD_BASIS": json.dumps(values),
        "ANBUD_BASELINE_CALLS": str(tmp / "settings-calls.jsonl"),
        "ANBUD_BASELINE_HARD_STOP": "0.01",
        "ANBUD_BASELINE_PRICE": json.dumps(baseline_price()),
        "ANBUD_BASELINE_DRY": "1",
    }
    command = [
        bun,
        "--no-env-file",
        "--no-install",
        f"--tsconfig-override={app_root / 'tsconfig.json'}",
        f"--preload={PRELOAD}",
        str(SETTINGS_SCRIPT),
        str(app_root),
        str(out),
    ]
    try:
        proc = subprocess.run(command, env=env, cwd=tmp, capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError):
        raise BaselineError("the settings could not be built") from None
    if proc.returncode != 0 or not out.is_file():
        raise BaselineError(f"the settings could not be built (exit {proc.returncode})")
    return hashlib.sha256(out.read_bytes()).hexdigest()


def read_calls(calls_log: Path) -> dict[str, Any]:
    """Counts from the preload's log. Ids are kept as distinct values only."""
    out: dict[str, Any] = {
        "calls": 0,
        "calls_failed": 0,
        "calls_incomplete": 0,
        "cost_usd": 0.0,
        "in_flight_usd_at_stop": 0.0,
        "workspace_ids": set(),
        "organization_ids": set(),
        "env_names": [],
        "blocked_hosts": 0,
        "calls_waited": 0,  # calls that waited for room under the hard stop (`Room`)
        "wait_ms_max": 0,
    }
    lines = calls_log.read_text(encoding="utf-8").splitlines() if calls_log.is_file() else []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        event = record.get("event")
        if event == "start":
            out["env_names"] = list(record.get("env_names") or [])
        elif event == "call":
            out["calls"] += 1
            out["cost_usd"] += float(record.get("cost_usd") or 0)
            status = record.get("status")
            if not isinstance(status, int) or status >= 400:
                out["calls_failed"] += 1
            if record.get("incomplete"):
                out["calls_incomplete"] += 1
            ids = record.get("ids") or {}
            if ids.get("anthropic-workspace-id"):
                out["workspace_ids"].add(ids["anthropic-workspace-id"])
            if ids.get("anthropic-organization-id"):
                out["organization_ids"].add(ids["anthropic-organization-id"])
        elif event == "hard_stop":
            out["in_flight_usd_at_stop"] = float(record.get("in_flight_usd") or 0)
        elif event == "blocked":
            out["blocked_hosts"] += 1
        elif event == "wait":
            out["calls_waited"] += 1
            out["wait_ms_max"] = max(out["wait_ms_max"], int(record.get("ms") or 0))
    out["workspace_ids"] = sorted(out["workspace_ids"])
    out["organization_ids"] = sorted(out["organization_ids"])
    return out


def run_engine(
    command: list[str],
    env: dict[str, str],
    *,
    cwd: Path,
    log_file: Path,
    timeout_s: float,
    stop_requested: Callable[[], bool],
) -> tuple[str, int | None, float]:
    """Run the engine to its end, a stop marker or the timeout. Returns (outcome, exit code, seconds)."""
    started = time.monotonic()
    with log_file.open("ab") as log:
        os.chmod(log_file, 0o600)
        proc = subprocess.Popen(command, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        outcome: str | None = None
        while True:
            try:
                code = proc.wait(timeout=POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                if stop_requested():
                    outcome = "stopped"
                elif time.monotonic() - started > timeout_s:
                    outcome = "timeout"
                else:
                    continue
                proc.terminate()
                try:
                    code = proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    code = proc.wait()
                break
    seconds = time.monotonic() - started
    return outcome or EXIT_CODES.get(code, "failed"), code, seconds


def run_baseline(
    *,
    run_id: str,
    documents: list[Path],
    outputs_dir: Path,
    record_dir: Path,
    app_root: Path,
    hard_stop: float,
    timeout_s: float,
    dry_run: bool,
    api_key: Callable[[], str],
    manifest: dict[str, Any],
    stop_requested: Callable[[], bool],
    settings: dict[str, Any] | None = None,
    bun: str | None = None,
) -> Outcome:
    """One engine run. The caller has checked who may run and where outputs go; this copies the documents into a
    work folder inside the run record, runs the engine and removes the work folder again. With `settings` (a
    basis from `matched_basis`), the engine prices on them through `record/settings.json`, whose hash the record
    keeps; without, on the script's own example settings."""
    check_run_id(run_id)
    if not hard_stop > 0:
        raise BaselineError("the hard stop must be above $0")
    bun = bun or shutil.which("bun")
    if not bun:
        raise BaselineError("bun isn't on PATH")
    private_dirs(record_dir)
    private_dirs(outputs_dir)
    work = record_dir / "work"
    shutil.rmtree(work, ignore_errors=True)
    private_dirs(work / "tmp")
    calls_log = record_dir / "calls.jsonl"
    out_file = outputs_dir / "engine.json"
    settings_file = record_dir / "settings.json" if settings is not None else None
    if settings is None:
        manifest = {**manifest, "settings": {"basis": "app-defaults"}}
    try:
        if settings_file is not None and settings is not None:
            sha = write_settings(bun, app_root, settings["values"], settings_file, work / "tmp")
            manifest = {**manifest, "settings": {"basis": "matched", **settings, "sha256": sha}}
        count, skipped = flat_documents(documents, work / "documents")
        if not count:
            raise BaselineError("no PDF, DOCX or XLSX file to read")
        _write_json(
            record_dir / "run.json", {**manifest, "status": "running", "documents": count, "skipped_by_type": skipped}
        )
        key = "dry-run-no-key" if dry_run else api_key()
        env = engine_env(bun, key, calls_log, hard_stop, baseline_price(), dry_run, work / "tmp")
        del key
        outcome, code, seconds = run_engine(
            engine_command(bun, app_root, work / "documents", out_file, settings_file),
            env,
            cwd=work,
            log_file=record_dir / "engine.log",
            timeout_s=timeout_s,
            stop_requested=stop_requested,
        )
        env.clear()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    counts = read_calls(calls_log)
    result = Outcome(
        run_id=run_id,
        outcome=outcome if outcome != "ok" or out_file.is_file() else "failed",
        exit_code=code,
        documents=count,
        skipped_by_type=skipped,
        duration_s=round(seconds, 1),
        output_file=out_file.is_file(),
        **counts,
    )
    result.cost_usd = round(result.cost_usd, 4)
    _write_json(record_dir / "run.json", {**manifest, "status": result.outcome, "result": asdict(result)})
    if not result.output_file:
        shutil.rmtree(outputs_dir, ignore_errors=True)
    return result


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def new_run_id(tender_id: str, app_sha: str, dry_run: bool, now: datetime | None = None, basis: str = "matched") -> str:
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    tag = "matched" if basis == "matched" else "defaults"
    return f"baseline-{tender_id}-{app_sha[:7]}-{tag}-{stamp}{'-dry' if dry_run else ''}"


def run_dev(
    company_id: str,
    tender_id: str,
    *,
    dry_run: bool,
    hard_stop: float | None = None,
    app: Path = APP_DIR,
    app_ref: str = APP_REF,
    code: dict[str, Any],
    stop_requested: Callable[[Path], bool],  # given the run's stop marker
    basis: str = "matched",
    api_key: Callable[[], str] | None = None,
    root: Path | None = None,
    now: datetime | None = None,
) -> Outcome:
    """The baseline on one dev tender (the caller has run the run guard and, for a paid run, the clean-tree check)."""
    budget = baseline_budget()
    cap = float(budget["hard_stop"])
    if hard_stop is not None and hard_stop > cap:
        raise BaselineError(f"--hard-stop may lower the ${cap:.2f} hard stop, never raise it")
    stop = cap if hard_stop is None else hard_stop
    if basis not in BASES:
        raise BaselineError(f"settings are one of: {', '.join(BASES)}")
    settings = matched_basis(root) if basis == "matched" else None
    sha = resolve_app(app, app_ref)
    app_root = export_app(app, sha, root)
    run_id = new_run_id(tender_id, sha, dry_run, now, basis)
    run_dir = paths.baseline_dir(company_id, tender_id, root) / run_id
    if run_dir.exists():
        raise BaselineError("a run with this id exists already")
    record_dir, outputs_dir = run_dir / "record", run_dir / "outputs"
    marker = paths.company_dir(company_id, root) / "runs" / run_id / "STOP"  # where `anbud stop <co> <run>` writes
    if api_key is None:
        from anbud.model_client import keychain_key

        def api_key() -> str:
            return keychain_key(account=load_config().workspace)

    manifest = {
        "kind": "baseline",
        "run_id": run_id,
        "tender": f"{company_id}/{tender_id}",
        "model": MODEL,
        "dry_run": dry_run,
        "hard_stop_usd": stop,
        "started_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds"),
        "code": {**code, "app_sha": sha, "app_ref": app_ref, "engine": SCRIPT},
    }
    return run_baseline(
        run_id=run_id,
        documents=dev_inbox_files(company_id, tender_id, root),
        outputs_dir=outputs_dir,
        record_dir=record_dir,
        app_root=app_root,
        hard_stop=stop,
        timeout_s=float(budget["timeout_minutes"]) * 60,
        dry_run=dry_run,
        api_key=api_key,
        manifest=manifest,
        stop_requested=lambda: stop_requested(marker),
        settings=settings,
    )


_TOTAL_LINE = re.compile(
    r"^Total: (?P<total>[\d\s]+) kr \(arbete (?P<labor>[\d\s]+), material (?P<material>[\d\s]+), "
    r"(?P<hours>[\d.,]+) h\)$"
)


def _kronor(text: str) -> int:
    return int(re.sub(r"\s", "", text))


def write_totals(run_dir: Path) -> Path:
    """`totals.json` beside the frozen folders: the engine's total and its labour, material and hours split, read by
    code from the line the script prints (sv-SE numbers) and checked against engine.json's total. Never printed."""
    engine = json.loads((run_dir / "outputs" / "engine.json").read_text(encoding="utf-8"))
    found = None
    for line in (run_dir / "record" / "engine.log").read_text(encoding="utf-8", errors="replace").splitlines():
        if match := _TOTAL_LINE.match(line.strip()):
            found = match
    out: dict[str, Any] = {"total_sek": engine.get("total"), "rows": len(engine.get("rows") or [])}
    if found:
        out |= {
            "labor_sek": _kronor(found["labor"]),
            "material_sek": _kronor(found["material"]),
            "hours": float(found["hours"].replace(",", ".")),
            "printed_total_matches": abs(_kronor(found["total"]) - round(float(engine.get("total") or 0))) <= 1,
        }
    target = run_dir / "totals.json"
    _write_json(target, out)
    return target
