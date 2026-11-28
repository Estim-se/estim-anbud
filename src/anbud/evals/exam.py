"""`anbud eval heldout|prospective <co>`: the exam on sealed tenders. Aggregates only.

It runs only where `runguard.held_out_refusal()` allows: a person at a terminal, or the eval lane
(`ANBUD_EVAL_LANE=1`). Everything it writes stays under `sealed/`. Nothing it prints names a tender, a file or a
value from a document: counts, sums, costs and one deadline number.

Per tender of the split status asked for:

1. **Quarantine**, before any prep. The sealed delivery (`heldout/<co>/<t>/`, untouched since intake) is unpacked
   into a staging folder, and intake's own rules hold the answer keys (Q&A, amendments, award, files the notice lists
   under those headings, unreadable archives) in `sealed/answers/<co>/<t>/intake/`. Two inputs come out of it:
   - `granskning`: without any answer key, since the Q&A and amendments are Granskning's answer key;
   - `kalkyl`: the same plus the amendments, as a bidder would have priced, scored against the opening protocol.
   Each variant is a data root of its own, `sealed/exam/<co>/<kind>/<variant>/`, with the tender laid out as a dev
   tender there, so the engines run unchanged with `ANBUD_DATA_ROOT` pointing at it.
2. **Prep** of both variants ($0). A tender that stops is counted by why, from prep's one stop line (`prep_stop`).
   A stop on a security marking is kept in the tender's exam record: later passes neither quarantine nor prepare that
   tender again, and still count it as stopped. A person decides whether it may be processed at all.
3. **The estimate**, from prep sizes: each engine's own dry run (Granskning's prefix tokens, Kalkyl's cost range) and
   the old engine's measured cost per page, times a margin, per engine and in total, beside the worst case (every
   hard stop), and the prefix sizes: median, max, and how many are over the context and over a large-prefix mark.
   Without `--paid` the exam stops here, after the old engine's dry run on each input.
4. **Runs** (`--paid`, with a scoring plan registered with `anbud freeze plan` and a `--budget` at or above the
   estimate): Granskning on the granskning input, Kalkyl on the
   kalkyl input, and the old engine at matched settings on both (once when they're the same). A run is not started
   when it could take the spend past the budget. Each run is frozen into the sealed ledger with the variant it used,
   its settings, whether it came after the tender's deadline and how it ended (`EngineRun.status`): finished, halted
   by a limit, or failed. Every attempt is frozen; only a finished run can be a scored prediction.

For prospective tenders it prints the nearest deadline of the open ones, in days, and warns about every tender whose
deadline has passed: a run after the deadline isn't a prospective prediction any more.

No figure is printed for a group of fewer than two tenders (`MIN_GROUP`). Sizes, the selection, the prefix plan and the
estimate are kept per tender in its sealed exam record and printed as one total over every prepared tender of both
splits (`combine`), never per split; a split of fewer than two tenders prints counts of tenders only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tomllib
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anbud import intake, paths
from anbud import notice as notices
from anbud.config import CONFIG_DIR
from anbud.evals import baseline, freeze
from anbud.evals import split_heldout as sh
from anbud.stops import ESTIMATE, Stop, granskning_stop, granskning_tiers, kalkyl_stop, kalkyl_tiers, top
from anbud.store import private_dirs

KINDS = {"heldout": sh.HELDOUT, "prospective": sh.PROSPECTIVE}
VARIANTS = ("granskning", "kalkyl")
ENGINES = ("granskning", "kalkyl", "baseline")
KALKYL_GETS = ("amendment",)  # held kinds a bidder had when pricing; Q&A, award and unknown kinds stay held
VARIANT_LABELS = {
    "granskning": "without answer keys (Q&A, amendments, award)",
    "kalkyl": "with the amendments; without Q&A and award",
}
CHILD_ENV_DROP = ("ANBUD_EVAL_LANE", "ANBUD_DATA_ROOT")
MIN_GROUP = 2  # no figure for a group of fewer tenders
KEPT_STOPS = ("security_marking",)  # prep stops a later pass doesn't retry; the tender isn't read again


class ExamError(RuntimeError):
    """Raised with a reason that names no tender content."""


# --- Places -------------------------------------------------------------------------------------------------------


def exam_dir(company_id: str, kind: str, root: Path | None = None) -> Path:
    return (root or paths.data_root()) / "sealed" / "exam" / paths.check_company_id(company_id) / kind


def variant_root(company_id: str, kind: str, variant: str, root: Path | None = None) -> Path:
    return exam_dir(company_id, kind, root) / variant


def held_dir(company_id: str, tender_id: str, root: Path | None = None) -> Path:
    base = root or paths.data_root()
    return (
        base / "sealed" / "answers" / paths.check_company_id(company_id) / paths.check_tender_id(tender_id) / "intake"
    )


def _sealed_tender(company_id: str, tender_id: str, root: Path | None) -> Path:
    return (root or paths.data_root()) / "heldout" / company_id / paths.check_tender_id(tender_id)


def exam_config(config_dir: Path = CONFIG_DIR) -> dict[str, Any]:
    return dict(tomllib.loads((config_dir / "budgets.toml").read_text())["exam"])


def hard_stops(config_dir: Path = CONFIG_DIR) -> dict[str, float]:
    """Each engine's top hard stop: a tender's worst case when its dry run gives no size."""
    budgets = tomllib.loads((config_dir / "budgets.toml").read_text())
    return {
        "granskning": top(granskning_tiers(budgets)),
        "kalkyl": top(kalkyl_tiers(budgets)),
        "baseline": float(budgets["baseline"]["hard_stop"]),
    }


def tender_tier(engine: str, size: float | None, config_dir: Path = CONFIG_DIR) -> Stop | None:
    """The tier a tender's run opens at, from its dry run (`anbud.stops`): Granskning's from the estimated prefix,
    Kalkyl's from its high estimate times its margin. None when the dry run gave no size."""
    if size is None:
        return None
    budgets = tomllib.loads((config_dir / "budgets.toml").read_text())
    if engine == "granskning":
        return granskning_stop(budgets, int(size), ESTIMATE)
    return kalkyl_stop(kalkyl_tiers(budgets), size, float(budgets["kalkyl"]["margin"]))


def tender_stop(engine: str, size: float | None, config_dir: Path = CONFIG_DIR) -> float:
    """The most a tender's run may spend: its tier's stop (`tender_tier`), or the top tier's when the size is
    unknown."""
    stop = tender_tier(engine, size, config_dir)
    return hard_stops(config_dir)[engine] if stop is None else stop.usd


def tier_names(config_dir: Path = CONFIG_DIR) -> dict[str, list[str]]:
    """Each engine's tier names, smallest first."""
    budgets = tomllib.loads((config_dir / "budgets.toml").read_text())
    return {
        "granskning": [t.name for t in granskning_tiers(budgets)],
        "kalkyl": [t.name for t in kalkyl_tiers(budgets)],
    }


def _record_tier(size: dict[str, Any], engine: str, stop: Stop | None) -> None:
    size["tiers"][engine] = stop.tier if stop is not None else "unknown"
    size["above_top"][engine] = bool(stop is not None and stop.above_top)


# --- Deadlines ----------------------------------------------------------------------------------------------------


def deadline_of(company_id: str, tender_id: str, root: Path | None = None) -> datetime | None:
    """The bid deadline intake read from the notice, or None (no notice, no deadline, or a folder delivery)."""
    record = _sealed_tender(company_id, tender_id, root) / "intake.json"
    if record.is_symlink() or not record.is_file():
        return None
    try:
        value = json.loads(record.read_text(encoding="utf-8")).get("notice", {}).get("deadline")
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except (ValueError, AttributeError):
        return None


@dataclass
class Deadlines:
    open: int = 0
    passed: int = 0
    unknown: int = 0
    nearest_days: int | None = None  # of the open ones


def deadlines(found: dict[str, datetime | None], now: datetime) -> Deadlines:
    out = Deadlines()
    for deadline in found.values():
        if deadline is None:
            out.unknown += 1
        elif intake._is_open(deadline, now):
            out.open += 1
            days = (deadline.date() - now.date()).days
            out.nearest_days = days if out.nearest_days is None else min(out.nearest_days, days)
        else:
            out.passed += 1
    return out


# --- Quarantine ---------------------------------------------------------------------------------------------------


def _record_path(company_id: str, kind: str, tender_id: str, root: Path | None) -> Path:
    return exam_dir(company_id, kind, root) / "tenders" / f"{paths.check_tender_id(tender_id)}.json"


def kept_stop(company_id: str, kind: str, tender_id: str, root: Path | None = None) -> dict[str, Any] | None:
    """The tender's exam record when an earlier pass kept its prep stop (`KEPT_STOPS`), else None."""
    path = _record_path(company_id, kind, tender_id, root)
    if path.is_symlink() or not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return record if isinstance(record, dict) and record.get("prep_stop") in KEPT_STOPS else None


def keep_stop(company_id: str, kind: str, tender_id: str, record: dict[str, Any], stop: str, root: Path | None) -> None:
    record["prep_stop"] = stop
    intake._write_private(_record_path(company_id, kind, tender_id, root), json.dumps(record, indent=1, sort_keys=True))


def _delivery(company_id: str, tender_id: str, root: Path | None) -> tuple[str, Path]:
    """("zip", the original zip) for a zip delivery, ("folder", the tender folder) for a folder moved unopened."""
    base = _sealed_tender(company_id, tender_id, root)
    if base.is_symlink() or not base.is_dir():
        raise ExamError("a sealed tender's folder is missing or a link")
    originals = base / "originals"
    if (base / "intake.json").is_file() and originals.is_dir() and not originals.is_symlink():
        zips = [p for p in originals.iterdir() if p.suffix == ".zip" and p.is_file() and not p.is_symlink()]
        if len(zips) != 1:
            raise ExamError("a sealed zip delivery doesn't hold exactly one original zip")
        return "zip", zips[0]
    return "folder", base


def _fingerprint(kind: str, source: Path) -> str:
    if kind == "zip":
        return intake._sha256_file(source)
    return intake.folder_fingerprint(source)


def _ensure_dev(vroot: Path, company_id: str, tender_id: str) -> None:
    """The variant root's own split ledger lists the tender as dev, so the engines' run guard lets it run there."""
    ledger = paths.ledger_path(company_id, vroot)
    status = sh.load_ledger(ledger).status if ledger.exists() else {}
    if status.get(tender_id) != sh.DEV:
        sh.append_events(ledger, [sh._assign(tender_id, sh.DEV, "exam", ())])


def _catalogs(vroot: Path, root: Path | None) -> None:
    source = (root or paths.data_root()) / "catalogs"
    if not source.is_dir() or source.is_symlink():
        return
    private_dirs(vroot / "catalogs")
    for path in source.glob("*.json"):
        if path.is_file() and not path.is_symlink():
            shutil.copyfile(path, vroot / "catalogs" / path.name)


def quarantine(company_id: str, kind: str, tender_id: str, root: Path | None = None) -> dict[str, Any]:
    """Unpack one sealed tender, hold its answer keys and lay out both inputs. Done once per delivery: a tender whose
    inputs are already laid out from the same delivery is left as it is."""
    exam = exam_dir(company_id, kind, root)
    record_path = _record_path(company_id, kind, tender_id, root)
    source_kind, source = _delivery(company_id, tender_id, root)
    fingerprint = _fingerprint(source_kind, source)
    tenders = {
        v: paths.dev_tender_dir(company_id, tender_id, variant_root(company_id, kind, v, root)) for v in VARIANTS
    }
    if record_path.is_file():
        old = json.loads(record_path.read_text(encoding="utf-8"))
        if old.get("source_sha256") == fingerprint and all((t / "inbox.sha256").is_file() for t in tenders.values()):
            return old
    staging = exam / ".staging" / tender_id
    shutil.rmtree(staging, ignore_errors=True)
    private_dirs(staging)
    held_to = held_dir(company_id, tender_id, root)
    try:
        inbox = staging / "granskning"
        stats = intake.UnpackStats()
        if source_kind == "zip":
            try:
                intake._safe_unzip(source, inbox, stats)
                stats.archives_unpacked += 1
                intake.unpack_all(inbox, stats)
            except intake._Keep:
                stats.archives_kept += 1
                private_dirs(inbox)
            notice = notices.from_zip(source)
        else:
            shutil.copytree(source, inbox, symlinks=True, ignore=shutil.ignore_patterns("intake.json", "source.path"))
            for path in list(inbox.rglob("*")):
                if path.is_symlink():
                    path.unlink()
            intake.unpack_all(inbox, stats)
            notice = notices.Notice()
        shutil.rmtree(held_to, ignore_errors=True)
        held, lines = intake._hold_answers(inbox, held_to, notice)
        if lines:
            intake._write_private(held_to / "intake.sha256", "\n".join(lines) + "\n")
        kalkyl_inbox = staging / "kalkyl"
        shutil.copytree(inbox, kalkyl_inbox)
        given = 0
        for line in lines:
            sha, held_kind, rel = line.split("  ", 2)
            if held_kind not in KALKYL_GETS:
                continue
            stored = held_to / f"{sha}{Path(rel).suffix.casefold()[:16]}"
            target = kalkyl_inbox / rel
            if target.exists() or not stored.is_file():
                continue
            private_dirs(target.parent)
            shutil.copyfile(stored, target)
            given += 1
        manifests = {"granskning": intake._manifest(inbox), "kalkyl": intake._manifest(kalkyl_inbox)}
        for variant, folder in (("granskning", inbox), ("kalkyl", kalkyl_inbox)):
            vroot = variant_root(company_id, kind, variant, root)
            tender = tenders[variant]
            shutil.rmtree(tender, ignore_errors=True)
            private_dirs(tender)
            os.rename(folder, tender / "inbox")
            intake._write_private(tender / "inbox.sha256", manifests[variant])
            _ensure_dev(vroot, company_id, tender_id)
            _catalogs(vroot, root)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    record = {
        "tender": tender_id,
        "source": source_kind,
        "source_sha256": fingerprint,
        "held": held,
        "amendments_to_kalkyl": given,
        "same_input": manifests["granskning"] == manifests["kalkyl"],
        "files": {v: len(m.splitlines()) for v, m in manifests.items()},
        "unpack": asdict(stats),
        "variants": VARIANT_LABELS,
    }
    private_dirs(record_path.parent)
    intake._write_private(record_path, json.dumps(record, indent=1, sort_keys=True))
    return record


# --- Engines ------------------------------------------------------------------------------------------------------


@dataclass
class EngineRun:
    engine: str
    variant: str
    run_id: str
    exit_code: int | None
    ok: bool
    cost_usd: float | None  # None when the run didn't say; counted at its hard stop
    outputs_dir: Path | None = None
    record_dir: Path | None = None
    settings: dict[str, Any] | None = None
    halted: bool = False  # stopped by a limit: its hard stop, the spend cap, a stop marker or the timeout

    @property
    def status(self) -> str:
        """How the run ended, as its freeze line says: only a finished run can be a scored prediction."""
        return freeze.FINISHED if self.ok else freeze.HALTED if self.halted else freeze.FAILED


_PREFIX = re.compile(r"estimated prefix: (\d+) tokens")
_KALKYL_HIGH = re.compile(r"^cost_usd_est_high: ([\d.]+)$", re.M)
_KALKYL_COST = re.compile(r"^cost_usd: ([\d.]+)$", re.M)
# Kalkyl exits 0 when its calc is incomplete too; it finished only when it froze its own outputs. `halted` counts the
# calls a limit refused.
_KALKYL_FROZEN = re.compile(r"^frozen: True$", re.M)
_KALKYL_HALTED = re.compile(r"^halted: (\d+)$", re.M)
_GRANSKNING_SPENT = re.compile(r"budget spent \$([\d.]+) of")
# Granskning's stop when the prefix's fixed part and priority 1 exceed the prefix budget (anbud.granskning.budget),
# as `anbud run` writes it to stderr.
PREFIX_STOP = "anbud run: stopped: the prefix's fixed part"
PAID_REFUSED = 3  # `anbud run`'s exit code for a run refused or stopped before any paid call
RUN_HALTED = 5  # `anbud run`'s exit code for a run a limit stopped before it finished
BASELINE_HALTS = ("hard_stop", "stopped", "timeout")  # the old engine's outcomes that are a limit, not an error

# Why prep stopped, from the one line `anbud prep` writes to stderr when it stops (its messages name no file and quote
# no text). The first fragment found decides.
PREP_STOPS = (
    ("security marking", "security_marking"),
    ("block-id prefix", "id_collision"),
    ("held-out or sealed storage", "sealed_storage"),
    ("intake manifest", "manifest"),
    ("no inbox folder", "manifest"),
    ("a file in the inbox can't be read", "unreadable_file"),
    ("while reading the tender", "read_error"),
)


def prep_stop(code: int, stderr: str) -> str | None:
    """None when prep finished; else why it stopped: `refused` (the run guard), a category from `PREP_STOPS`,
    `other_stop` (a stop line with another reason) or `crash` (no stop line: an exception, a signal, the memory
    limit)."""
    if code == 0:
        return None
    line = next((x for x in reversed(stderr.splitlines()) if x.startswith("anbud prep: ")), None)
    if line is None:
        return "crash"
    if line.startswith("anbud prep: refused:"):
        return "refused"
    return next((category for fragment, category in PREP_STOPS if fragment in line), "other_stop")


class Engines:
    """The engines, each run as `anbud ...` in a child process whose data root is one variant root. A child's output
    goes to a log in sealed storage and is read here by code only."""

    def __init__(self, logs: Path, app: Path, app_ref: str, root: Path | None) -> None:
        self.logs = logs
        self.app = app
        self.app_ref = app_ref
        self.root = root
        self._app_root: Path | None = None
        self._app_sha: str | None = None

    def _anbud(self, vroot: Path, args: list[str], log_name: str) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k not in CHILD_ENV_DROP}
        env["ANBUD_DATA_ROOT"] = str(vroot)
        private_dirs(self.logs)
        log = self.logs / log_name
        command = [sys.executable, "-c", "import sys; from anbud.cli import main; sys.exit(main())", *args]
        proc = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
        intake._write_private(log, proc.stdout + "\n--- stderr ---\n" + proc.stderr)
        return proc

    def prep(self, vroot: Path, company_id: str, tender_id: str, log_name: str) -> str | None:
        """None when prep finished, else the category of its stop (`prep_stop`)."""
        proc = self._anbud(vroot, ["prep", company_id, tender_id], log_name)
        return prep_stop(proc.returncode, proc.stderr)

    def granskning_estimate(self, vroot: Path, company_id: str, tender_id: str, log_name: str) -> int | None:
        proc = self._anbud(vroot, ["run", company_id, tender_id, "--dry-run"], log_name)
        match = _PREFIX.search(proc.stdout)
        return int(match.group(1)) if proc.returncode == 0 and match else None

    def granskning_stop_check(self, vroot: Path, company_id: str, tender_id: str, run_id: str) -> bool:
        """Whether Granskning's run stops on the prefix budget before any model call, checked at $0: the run path in
        replay mode under a new run id, whose step cache is empty, so any model call is refused instead of sent."""
        proc = self._anbud(vroot, ["run", company_id, tender_id, "--replay", "--run-id", run_id], f"{run_id}.log")
        return proc.returncode == PAID_REFUSED and PREFIX_STOP in proc.stderr and "replay incomplete" not in proc.stderr

    def kalkyl_estimate(self, vroot: Path, company_id: str, tender_id: str, log_name: str) -> float | None:
        proc = self._anbud(vroot, ["kalkyl", company_id, tender_id, "--dry-run"], log_name)
        match = _KALKYL_HIGH.search(proc.stdout)
        return float(match.group(1)) if proc.returncode == 0 and match else None

    def granskning(self, vroot: Path, company_id: str, tender_id: str, run_id: str) -> EngineRun:
        proc = self._anbud(vroot, ["run", company_id, tender_id, "--run-id", run_id], f"{run_id}.log")
        code, match = proc.returncode, _GRANSKNING_SPENT.search(proc.stdout)
        return EngineRun(
            "granskning",
            "granskning",
            run_id,
            code,
            code == 0,
            float(match.group(1)) if match else None,
            paths.dev_tender_dir(company_id, tender_id, vroot) / "runs" / run_id,
            paths.company_dir(company_id, vroot) / "runs" / run_id,
            halted=code == RUN_HALTED,
        )

    def kalkyl(self, vroot: Path, company_id: str, tender_id: str, run_id: str) -> EngineRun:
        proc = self._anbud(vroot, ["kalkyl", company_id, tender_id, "--run-id", run_id], f"{run_id}.log")
        code, match = proc.returncode, _KALKYL_COST.search(proc.stdout)
        halted = _KALKYL_HALTED.search(proc.stdout)
        return EngineRun(
            "kalkyl",
            "kalkyl",
            run_id,
            code,
            code == 0 and _KALKYL_FROZEN.search(proc.stdout) is not None,
            float(match.group(1)) if match else None,
            paths.dev_tender_dir(company_id, tender_id, vroot) / "kalkyl" / run_id,
            paths.company_dir(company_id, vroot) / "runs" / run_id,
            halted=halted is not None and int(halted.group(1)) > 0,
        )

    def selection_plan(self, vroot: Path, company_id: str, tender_id: str) -> dict[str, Any] | None:
        """The selection and prefix plan a Granskning run on the granskning input would use (`selection_sizes`)."""
        tender = paths.dev_tender_dir(company_id, tender_id, vroot)
        budgets = tomllib.loads((CONFIG_DIR / "budgets.toml").read_text())
        return selection_sizes(tender / "prep", tender / "calc", budgets)

    def _app_export(self) -> Path:
        if self._app_root is None:
            self._app_sha = baseline.resolve_app(self.app, self.app_ref)
            self._app_root = baseline.export_app(self.app, self._app_sha, self.root)
        return self._app_root

    def baseline(
        self, vroot: Path, company_id: str, tender_id: str, variant: str, run_id: str, run_dir: Path, *, dry_run: bool
    ) -> EngineRun:
        from anbud.config import load_config
        from anbud.granskning import provenance
        from anbud.model_client import keychain_key

        app_root = self._app_export()
        settings = baseline.matched_basis(vroot)
        tender = paths.dev_tender_dir(company_id, tender_id, vroot)
        files = [
            tender / "inbox" / line.split("  ", 1)[1] for line in (tender / "inbox.sha256").read_text().splitlines()
        ]
        manifest = {
            "kind": "baseline",
            "run_id": run_id,
            "tender": f"{company_id}/{tender_id}",
            "model": baseline.MODEL,
            "dry_run": dry_run,
            "exam_variant": variant,
            "code": {
                **provenance.code_block(provenance.git_state()),
                "app_sha": self._app_sha,
                "engine": baseline.SCRIPT,
            },
        }
        try:
            result = baseline.run_baseline(
                run_id=run_id,
                documents=files,
                outputs_dir=run_dir / "outputs",
                record_dir=run_dir / "record",
                app_root=app_root,
                hard_stop=hard_stops()["baseline"],
                timeout_s=float(tomllib.loads((CONFIG_DIR / "budgets.toml").read_text())["baseline"]["timeout_minutes"])
                * 60,
                dry_run=dry_run,
                api_key=lambda: keychain_key(account=load_config().workspace),
                manifest=manifest,
                stop_requested=lambda: (paths.data_root() / "STOP").exists(),
                settings=settings,
            )
        except baseline.BaselineError:
            return EngineRun("baseline", variant, run_id, None, False, 0.0)
        ok = result.outcome == ("dry_run" if dry_run else "ok")
        recorded = json.loads((run_dir / "record" / "run.json").read_text(encoding="utf-8")).get("settings")
        return EngineRun(
            "baseline",
            variant,
            run_id,
            result.exit_code,
            ok,
            result.cost_usd,
            run_dir / "outputs",
            run_dir / "record",
            recorded,
            halted=result.outcome in BASELINE_HALTS,
        )


def selection_sizes(prep_dir: Path, calc_dir: Path, budgets: dict[str, Any]) -> dict[str, Any] | None:
    """Counts from `anbud.selection` and the prefix budget (`anbud.granskning.budget`), with the same calls a
    Granskning run makes: documents and pages, the pages left out as other trades', unknown documents, and the
    planned prefix. None when the prep output can't be read."""
    from anbud.granskning.budget import PrefixBudget, plan_prefix
    from anbud.granskning.calc import load_calc
    from anbud.selection import SelectionError, load_selected

    try:
        corpus, selection = load_selected(prep_dir)
        calc = load_calc(calc_dir)
        budget = PrefixBudget.from_config(budgets)
        plan = plan_prefix(corpus, calc, budget, corpus.tiles.values()) if budget is not None else None  # as a run
    except (OSError, ValueError, KeyError, SelectionError):
        return None
    out: dict[str, Any] = {
        "documents": len(selection.classes),
        "unknown_documents": sum(c.discipline == "unknown" for c in selection.classes.values()),
        "pages": sum(selection.pages.values()),
        "pages_excluded": selection.pages_excluded,
    }
    if plan is not None:
        out |= {
            "budget_tokens": plan.budget,
            "p1_fits": plan.p1_fits,
            "stops": plan.stops,  # the core (AF and contract) doesn't fit: the run stops before any paid call
            "planned_tokens": plan.total,
            "search_only": len(plan.search_only),
            "p1_search_only": len(plan.p1_search_only),  # of them, P1 documents read through search
            "core_search_only": len(plan.core_search_only),  # of those, the AF or the contract: a partial review
        }
    return out


def _stops(selection: dict[str, Any]) -> bool:
    """Whether a tender's run stops at its prefix check: not even every document's outline fits. A size record from
    before P1 could be demoted has no `stops`: then P1 not fitting was the stop."""
    return bool(selection.get("stops", selection.get("p1_fits") is False))


# --- The exam -----------------------------------------------------------------------------------------------------


@dataclass
class Report:
    kind: str
    tenders: int = 0
    quarantined: int = 0
    held_tenders: int = 0
    held: dict[str, int] = field(default_factory=dict)
    amendments_to_kalkyl: int = 0
    same_input: int = 0
    prepared: int = 0
    prep_failed: int = 0
    prep_stops: dict[str, int] = field(default_factory=dict)  # by category, one per tender that stopped
    prep_stops_kept: int = 0  # of those, stops kept from an earlier pass: the tender wasn't read again
    documents: int = 0
    skipped_by_type: dict[str, int] = field(default_factory=dict)
    pages: int = 0
    # Granskning dry runs: runs, median and max estimated prefix, runs over the context and over the large mark.
    prefix_tokens: dict[str, int] = field(default_factory=dict)
    deadlines: Deadlines | None = None
    estimate_usd: dict[str, float] = field(default_factory=dict)
    estimate_unknown: int = 0
    worst_case_usd: float = 0.0
    baseline_runs: int = 0
    dry_runs_ok: dict[str, int] = field(default_factory=dict)
    dry_runs: dict[str, int] = field(default_factory=dict)
    paid: bool = False
    plan: dict[str, Any] | None = None
    runs_ok: dict[str, int] = field(default_factory=dict)  # finished runs, by engine
    runs_halted: dict[str, int] = field(default_factory=dict)  # stopped by a limit, by engine
    runs_failed: dict[str, int] = field(default_factory=dict)
    runs_skipped_budget: int = 0
    runs_prefix_stop: int = 0  # Granskning runs not started: they stop on the prefix budget, checked at $0
    runs_after_deadline: int = 0
    spent_usd: float = 0.0
    frozen: int = 0
    frozen_by_status: dict[str, int] = field(default_factory=dict)
    not_frozen: int = 0  # runs that left no run record, or whose freeze was refused
    combined: dict[str, Any] = field(default_factory=dict)  # one total over both splits (`combine`)

    @property
    def estimate_total(self) -> float:
        return round(sum(self.estimate_usd.values()), 2)


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _summary(tender: Path) -> dict[str, Any]:
    path = tender / "prep" / "summary.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _skipped(tender: Path) -> Counter[str]:
    """Prep's skipped files by type, from its documents.json (each entry's kind; the reason is never read)."""
    try:
        documents = json.loads((tender / "prep" / "documents.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Counter()
    return Counter(str(d.get("kind")) for d in documents if isinstance(d, dict) and d.get("skipped"))


def prefix_stats(tokens: list[int], context: int, large: int) -> dict[str, int]:
    if not tokens:
        return {}
    return {
        "runs": len(tokens),
        "median": round(statistics.median(tokens)),
        "max": max(tokens),
        "over_context": sum(t > context for t in tokens),
        "over_large": sum(t > large for t in tokens),
        "context": context,
        "large": large,
    }


def run_exam(
    company_id: str,
    kind: str,
    *,
    paid: bool = False,
    budget_usd: float | None = None,
    engines: tuple[str, ...] = ENGINES,
    app: Path = baseline.APP_DIR,
    app_ref: str = baseline.APP_REF,
    runner: Engines | None = None,
    now: datetime | None = None,
    local_now: datetime | None = None,
    root: Path | None = None,
    say: Callable[[str], None] = print,
) -> Report:
    """The exam. `say` gets aggregate lines only. Raises ExamError before anything is unpacked when it can't start."""
    if kind not in KINDS:
        raise ExamError("the exam is heldout or prospective")
    if paid and (budget_usd is None or budget_usd <= 0):
        raise ExamError("a paid exam needs --budget, in USD")
    unknown = set(engines) - set(ENGINES)
    if unknown:
        raise ExamError(f"engines are {', '.join(ENGINES)}")
    plan: dict[str, Any] | None = None
    if paid:
        try:
            plan = freeze.current_plan(company_id, root)
        except freeze.FreezeError as exc:
            raise ExamError(str(exc)) from None
        if plan is None:
            raise ExamError("a paid exam needs a scoring plan registered first: anbud freeze plan <co> <file>")
    now = now or datetime.now(UTC)
    local_now = local_now or datetime.now()
    try:
        status = sh.load_ledger(paths.ledger_path(company_id, root)).status
    except sh.SplitError as exc:
        raise ExamError(str(exc)) from None
    tenders = sorted(t for t, s in status.items() if s == KINDS[kind])
    report = Report(kind=kind, tenders=len(tenders), paid=paid)
    if plan is not None:
        report.plan = {"file": plan["file"], "sha256": plan["sha256"], "line": plan["line_number"]}
    exam = exam_dir(company_id, kind, root)
    stamp = _stamp(now)
    logs = exam / "logs" / stamp
    runner = runner or Engines(logs, app, app_ref, root)
    cfg = exam_config()
    stops = hard_stops()
    found = {t: deadline_of(company_id, t, root) for t in tenders}
    if kind == "prospective":
        report.deadlines = deadlines(found, local_now)
    after = {t: d is not None and not intake._is_open(d, local_now) for t, d in found.items()}

    # 1-2. Quarantine and prep.
    records: dict[str, dict[str, Any]] = {}
    ready: list[str] = []
    for t in tenders:
        kept = kept_stop(company_id, kind, t, root)
        record = kept or quarantine(company_id, kind, t, root)
        records[t] = record
        report.quarantined += 1
        report.held_tenders += bool(record["held"])
        for k, n in record["held"].items():
            report.held[k] = report.held.get(k, 0) + n
        report.amendments_to_kalkyl += record["amendments_to_kalkyl"]
        report.same_input += record["same_input"]
        stop = None
        if kept is not None:
            stop = str(kept["prep_stop"])
            report.prep_stops_kept += 1
        else:
            for v in VARIANTS:
                stop = runner.prep(variant_root(company_id, kind, v, root), company_id, t, f"prep-{t}-{v}.log")
                if stop is not None:
                    break
            if stop in KEPT_STOPS:
                keep_stop(company_id, kind, t, record, stop, root)
        if stop is not None:
            report.prep_stops[stop] = report.prep_stops.get(stop, 0) + 1
        ok = stop is None
        report.prepared += ok
        report.prep_failed += not ok
        if ok:
            ready.append(t)
            tender = paths.dev_tender_dir(company_id, t, variant_root(company_id, kind, "granskning", root))
            summary = _summary(tender)
            report.documents += int(summary.get("documents") or 0)
            for k, n in _skipped(tender).items():
                report.skipped_by_type[k] = report.skipped_by_type.get(k, 0) + n
            report.pages += sum((summary.get("pages_by_kind") or {}).values())

    # 3. The estimate, from the engines' own dry runs and prep sizes.
    # Granskning's basis is the planned prefix when the prefix budget applies: what a paid run would send. A tender
    # whose fixed part and priority 1 don't fit would stop before any call, but is counted at its hard stop, as the
    # paid loop counts a run that reports no cost.
    margin = float(cfg["margin"])
    estimate: Counter[str] = Counter()
    plans: dict[str, list[str]] = {}
    prefixes: list[int] = []
    sizes: dict[str, dict[str, Any]] = {}
    caps: dict[tuple[str, str], float] = {}  # (tender, engine) -> the most its run may spend
    prefix_stopped: set[str] = set()  # tenders whose Granskning run stops on the prefix budget, checked at $0
    for t in ready:
        roots = {v: variant_root(company_id, kind, v, root) for v in VARIANTS}
        tender = paths.dev_tender_dir(company_id, t, roots["granskning"])
        summary = _summary(tender)
        selection = runner.selection_plan(roots["granskning"], company_id, t)
        size: dict[str, Any] = {
            "at": stamp,
            "documents": int(summary.get("documents") or 0),
            "pages": sum((summary.get("pages_by_kind") or {}).values()),
            "skipped_by_type": dict(sorted(_skipped(tender).items())),
            "prefix_tokens": None,
            "selection": selection,
            "estimate_usd": {},
            "estimate_unknown": 0,
            "worst_usd": 0.0,
            "baseline_runs": 0,
            "tiers": {},  # engine -> the tier its run opens at, or "unknown" (the top tier's stop)
            "above_top": {},  # engine -> whether the size is above the top tier
        }
        if "granskning" in engines:
            tokens = runner.granskning_estimate(roots["granskning"], company_id, t, f"estimate-{t}-granskning.log")
            size["prefix_tokens"] = tokens
            fits = bool(selection and "p1_fits" in selection and not _stops(selection))
            planned = selection.get("planned_tokens") if selection and fits else None
            basis = planned if planned is not None else tokens
            cap = caps[t, "granskning"] = tender_stop("granskning", tokens)  # the tier its run opens at
            _record_tier(size, "granskning", tender_tier("granskning", tokens))
            if tokens is not None:
                prefixes.append(tokens)
            would_stop = bool(selection and _stops(selection))
            if would_stop:  # checked at $0; a confirmed stop is never started, so it costs nothing
                check_id = f"exam-{kind}-{t}-granskning-prefixcheck-{stamp}"
                stopped = runner.granskning_stop_check(roots["granskning"], company_id, t, check_id)
                size["selection"] = {**(selection or {}), "stop_checked": stopped}
                if stopped:
                    prefix_stopped.add(t)
            if t in prefix_stopped:
                usd = cap = 0.0
            elif basis is None or would_stop:
                size["estimate_unknown"] += basis is None
                usd = cap
            else:
                usd = min(basis / 1e6 * float(cfg["granskning_usd_per_mtok_prefix"]) * margin, cap)
            size["estimate_usd"]["granskning"] = usd
            size["worst_usd"] += cap
        if "kalkyl" in engines:
            high = runner.kalkyl_estimate(roots["kalkyl"], company_id, t, f"estimate-{t}-kalkyl.log")
            cap = caps[t, "kalkyl"] = tender_stop("kalkyl", high)
            _record_tier(size, "kalkyl", tender_tier("kalkyl", high))
            size["estimate_unknown"] += high is None
            size["estimate_usd"]["kalkyl"] = cap if high is None else min(high * margin, cap)
            size["worst_usd"] += cap
        variants = ["granskning"] if records[t]["same_input"] else list(VARIANTS)
        plans[t] = variants if "baseline" in engines else []
        for v in plans[t]:
            pages = sum((_summary(paths.dev_tender_dir(company_id, t, roots[v])).get("pages_by_kind") or {}).values())
            usd = min(pages * float(cfg["baseline_usd_per_page"]) * margin, stops["baseline"])
            size["estimate_usd"]["baseline"] = size["estimate_usd"].get("baseline", 0.0) + usd
            size["worst_usd"] += stops["baseline"]
            size["baseline_runs"] += 1
        estimate.update(size["estimate_usd"])
        report.estimate_unknown += size["estimate_unknown"]
        report.worst_case_usd += size["worst_usd"]
        report.baseline_runs += size["baseline_runs"]
        sizes[t] = size
    report.estimate_usd = {k: round(v, 2) for k, v in sorted(estimate.items())}
    report.worst_case_usd = round(report.worst_case_usd, 2)
    report.prefix_tokens = prefix_stats(prefixes, int(cfg["context_tokens"]), int(cfg["large_prefix_tokens"]))
    for t in tenders:  # each tender's record holds the sizes of its last pass, and none once prep stops
        record = dict(records[t])
        if t in sizes:
            record["sizes"] = sizes[t]
        elif "sizes" not in record:
            continue
        else:
            del record["sizes"]
        records[t] = record
        intake._write_private(_record_path(company_id, kind, t, root), json.dumps(record, indent=1, sort_keys=True))
    report.combined = combine(company_id, status, root)

    # The old engine's dry run on each input: the plumbing works, and nothing is sent.
    if not paid:
        for t in ready:
            for v in plans[t]:
                run_id = f"exam-{kind}-{t}-baseline-{v}-{stamp}-dry"
                one = runner.baseline(
                    variant_root(company_id, kind, v, root),
                    company_id,
                    t,
                    v,
                    run_id,
                    exam / "baseline" / t / run_id,
                    dry_run=True,
                )
                report.dry_runs["baseline"] = report.dry_runs.get("baseline", 0) + 1
                report.dry_runs_ok["baseline"] = report.dry_runs_ok.get("baseline", 0) + one.ok
        _write_exam(exam, stamp, report, records, [])
        _say(report, say)
        return report

    # 4. Paid runs, within the budget.
    assert budget_usd is not None
    if report.estimate_total > budget_usd:
        figure = f" (${report.estimate_total:.2f})" if report.tenders >= MIN_GROUP else ""
        raise ExamError(f"the estimate{figure} is above the budget (${budget_usd:.2f})")
    runs: list[EngineRun] = []
    for t in ready:
        roots = {v: variant_root(company_id, kind, v, root) for v in VARIANTS}
        todo: list[tuple[str, str]] = [(e, e) for e in ("granskning", "kalkyl") if e in engines]
        todo += [("baseline", v) for v in plans[t]]
        for engine, variant in todo:
            if engine == "granskning" and t in prefix_stopped:
                report.runs_prefix_stop += 1  # it would stop before any call: nothing to run, nothing to freeze
                continue
            if report.spent_usd + caps.get((t, engine), stops[engine]) > budget_usd:
                report.runs_skipped_budget += 1
                continue
            run_id = f"exam-{kind}-{t}-{engine}{'-' + variant if engine == 'baseline' else ''}-{stamp}"
            if engine == "granskning":
                one = runner.granskning(roots[variant], company_id, t, run_id)
            elif engine == "kalkyl":
                one = runner.kalkyl(roots[variant], company_id, t, run_id)
            else:
                one = runner.baseline(
                    roots[variant], company_id, t, variant, run_id, exam / "baseline" / t / run_id, dry_run=False
                )
            report.spent_usd += caps.get((t, engine), stops[engine]) if one.cost_usd is None else one.cost_usd
            status = one.status
            bucket = {freeze.FINISHED: report.runs_ok, freeze.HALTED: report.runs_halted}.get(
                status, report.runs_failed
            )
            bucket[engine] = bucket.get(engine, 0) + 1
            runs.append(one)
            if one.outputs_dir is None or one.record_dir is None:
                report.not_frozen += 1  # it left nothing to freeze
                continue
            if one.ok:
                report.runs_after_deadline += after[t]
            extra = {
                "exam": {
                    "kind": kind,
                    "engine": engine,
                    "variant": variant,
                    "input": VARIANT_LABELS[variant],
                    "after_deadline": after[t],
                    "settings": one.settings,
                    "plan": report.plan,
                }
            }
            try:
                freeze.freeze_run(
                    company_id,
                    run_id,
                    outputs_dir=one.outputs_dir,
                    run_record_dir=one.record_dir,
                    tender_id=t,
                    sealed=True,
                    input_dir=paths.dev_tender_dir(company_id, t, roots[variant]),
                    extra=extra,
                    root=root,
                    status=status,
                )
                report.frozen += 1
                report.frozen_by_status[status] = report.frozen_by_status.get(status, 0) + 1
            except freeze.FreezeError:
                report.not_frozen += 1
                if one.ok:  # a finished run that isn't frozen can't be scored
                    bucket[engine] -= 1
                    report.runs_failed[engine] = report.runs_failed.get(engine, 0) + 1
    report.spent_usd = round(report.spent_usd, 2)
    _write_exam(exam, stamp, report, records, runs)
    _say(report, say)
    return report


def _median(values: list[int]) -> int:
    return round(statistics.median(values))


def _tier_counts(found: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per engine, tenders per tier in tier order (unknown last, when any), and how many are above the top tier."""
    out: dict[str, dict[str, Any]] = {}
    for engine, names in tier_names().items():
        got = [one["tiers"][engine] for one in found if engine in (one.get("tiers") or {})]
        if not got:
            continue
        counts = {name: got.count(name) for name in names}
        if "unknown" in got:
            counts["unknown"] = got.count("unknown")
        above = sum(bool((one.get("above_top") or {}).get(engine)) for one in found)
        out[engine] = {"counts": counts, "above_top": above}
    return out


def combine(company_id: str, status: dict[str, str], root: Path | None = None) -> dict[str, Any]:
    """Sizes, the selection, the prefix plan and the estimate as one total over every tender of both splits whose
    last exam pass prepared it, from the sealed tender records. Never per tender or per split; `tenders` and
    `by_split` are counts of tenders."""
    found: list[dict[str, Any]] = []
    by_split: Counter[str] = Counter()
    for kind, split in KINDS.items():
        for t in sorted(x for x, s in status.items() if s == split):
            path = _record_path(company_id, kind, t, root)
            if path.is_symlink() or not path.is_file():
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                continue
            one = record.get("sizes") if isinstance(record, dict) else None
            if isinstance(one, dict):
                found.append(one)
                by_split[kind] += 1
    out: dict[str, Any] = {"tenders": len(found), "by_split": dict(sorted(by_split.items()))}
    if not found:
        return out
    cfg = exam_config()
    skipped: Counter[str] = Counter()
    estimate: Counter[str] = Counter()
    for one in found:
        skipped.update(one.get("skipped_by_type") or {})
        estimate.update(one.get("estimate_usd") or {})
    out |= {
        "documents": sum(int(one.get("documents") or 0) for one in found),
        "pages": sum(int(one.get("pages") or 0) for one in found),
        "skipped_by_type": dict(sorted(skipped.items())),
        "prefix": prefix_stats(
            [int(one["prefix_tokens"]) for one in found if one.get("prefix_tokens") is not None],
            int(cfg["context_tokens"]),
            int(cfg["large_prefix_tokens"]),
        ),
        "estimate_usd": {k: round(v, 2) for k, v in sorted(estimate.items())},
        "estimate_unknown": sum(int(one.get("estimate_unknown") or 0) for one in found),
        "worst_usd": round(sum(float(one.get("worst_usd") or 0) for one in found), 2),
        "baseline_runs": sum(int(one.get("baseline_runs") or 0) for one in found),
        "tiers": _tier_counts(found),
    }
    selections = [one["selection"] for one in found if isinstance(one.get("selection"), dict)]
    if selections:
        pages = sum(int(x["pages"]) for x in selections)
        documents = sum(int(x["documents"]) for x in selections)
        selected = [int(x["pages"]) - int(x["pages_excluded"]) for x in selections]
        planned = [x for x in selections if "p1_fits" in x]
        fitting = [int(x["planned_tokens"]) for x in planned if not _stops(x)]
        out["selection"] = {
            "tenders": len(selections),
            "pages_excluded_pct": round(100 * sum(int(x["pages_excluded"]) for x in selections) / pages, 1)
            if pages
            else 0.0,
            "unknown_documents_pct": round(100 * sum(int(x["unknown_documents"]) for x in selections) / documents, 1)
            if documents
            else 0.0,
            "selected_pages_median": _median(selected),
            "selected_pages_max": max(selected),
            "planned": len(planned),
            "budget_tokens": max((int(x["budget_tokens"]) for x in planned), default=0),
            "would_stop": sum(_stops(x) for x in planned),
            "stop_checked": sum(bool(x.get("stop_checked")) for x in planned if _stops(x)),
            "fitting": len(fitting),
            "planned_tokens_median": _median(fitting) if fitting else None,
            "planned_tokens_max": max(fitting) if fitting else None,
            "search_only_documents": sum(int(x.get("search_only") or 0) for x in planned),
            "p1_search_only_documents": sum(int(x.get("p1_search_only") or 0) for x in planned),
            "core_search_only_documents": sum(int(x.get("core_search_only") or 0) for x in planned),
            "partial": sum(bool(x.get("core_search_only")) for x in planned if not _stops(x)),
        }
    return out


def _write_exam(
    exam: Path, stamp: str, report: Report, records: dict[str, dict[str, Any]], runs: list[EngineRun]
) -> None:
    """The exam's own record, in sealed storage: per tender and per run, ids and counts."""
    data = {
        "report": asdict(report),
        "tenders": records,
        "runs": [{**asdict(r), "status": r.status, "outputs_dir": None, "record_dir": None} for r in runs],
    }
    target = exam / "exams" / f"{stamp}.json"
    private_dirs(target.parent)
    intake._write_private(target, json.dumps(data, indent=1, sort_keys=True, default=str))


def _counts(values: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(values.items())) or "none"


def _say(r: Report, say: Callable[[str], None]) -> None:
    """Aggregates only, and no figure for a group of fewer than `MIN_GROUP` tenders."""
    small = r.tenders < MIN_GROUP
    say(f"anbud eval {r.kind}: {r.tenders} tenders" + ("; fewer than 2, so counts of tenders only" if small else ""))
    if not small:
        say(
            f"quarantine: answer keys held in {r.held_tenders} of {r.quarantined} ({_counts(r.held)}); "
            f"{r.amendments_to_kalkyl} amendments given to Kalkyl; {r.same_input} tenders with one input for both"
        )
    kept = f"; {r.prep_stops_kept} kept from an earlier pass, not read again" if r.prep_stops_kept else ""
    stops = f" ({_counts(r.prep_stops)}{kept})" if r.prep_stops else ""
    say(f"prep: {r.prepared} prepared, {r.prep_failed} stopped{stops}")
    if r.deadlines is not None:
        d = r.deadlines
        if small:
            say(f"deadlines: {d.open} open; {d.passed} passed; {d.unknown} unknown")
        else:
            nearest = f"nearest open deadline in {d.nearest_days} days" if d.nearest_days is not None else "none open"
            say(f"deadlines: {d.open} open, {nearest}; {d.passed} passed; {d.unknown} unknown")
        if d.passed:
            say(f"warning: {d.passed} tenders are past their deadline; a run now comes after it")
    for engine in sorted(r.dry_runs):
        ok, total = r.dry_runs_ok.get(engine, 0), r.dry_runs[engine]
        if small:
            say(f"dry runs: {engine} {'all' if ok == total else 'not all'} reached the first model call")
        else:
            say(f"dry runs: {engine} {ok} of {total} reached the first model call")
    if r.plan is not None:
        say(f"scoring plan: ledger line {r.plan['line']}, sha256 {r.plan['sha256'][:12]}")
    if r.paid:
        if small:
            say("runs: counts and spend stay in the sealed exam record (fewer than 2 tenders)")
        else:
            stopped = f"; {r.runs_prefix_stop} not started (prefix budget)" if r.runs_prefix_stop else ""
            unfrozen = f"; {r.not_frozen} not frozen" if r.not_frozen else ""
            say(
                f"runs: finished {_counts(r.runs_ok)}; halted {_counts(r.runs_halted)}; failed "
                f"{_counts(r.runs_failed)}; {r.runs_skipped_budget} not started (budget){stopped}; spent "
                f"${r.spent_usd:.2f}; {r.frozen} frozen in the sealed ledger ({_counts(r.frozen_by_status)}){unfrozen}"
            )
        if r.runs_after_deadline:
            say(f"warning: {r.runs_after_deadline} frozen runs came after their tender's deadline")
    _say_combined(r.combined, say)


def _say_combined(c: dict[str, Any], say: Callable[[str], None]) -> None:
    """The total over both splits, or a note when fewer than `MIN_GROUP` tenders have sizes."""
    if c.get("tenders", 0) < MIN_GROUP:
        say("both splits: fewer than 2 prepared tenders, so no sizes or estimates")
        return
    splits = ", ".join(f"{k} {v}" for k, v in c["by_split"].items())
    say(
        f"both splits, one total over {c['tenders']} prepared tenders ({splits}): {c['documents']} documents "
        f"(skipped by type: {_counts(c['skipped_by_type'])}), {c['pages']} pages (granskning input)"
    )
    p = c.get("prefix") or {}
    if p:
        say(
            "granskning prefix after the selection, before the prefix budget (dry-run estimate, before page cards): "
            f"median {p['median']:,} tokens, max {p['max']:,}; of {p['runs']} runs {p['over_context']} over the "
            f"context ({p['context']:,}), {p['over_large']} over {p['large']:,}"
        )
    sel = c.get("selection")
    if sel:
        say(
            f"selection: {sel['pages_excluded_pct']:.1f}% of pages left out as other trades', "
            f"{sel['unknown_documents_pct']:.1f}% of documents unknown; selected pages median "
            f"{sel['selected_pages_median']:,}, max {sel['selected_pages_max']:,}"
        )
        if sel["planned"]:
            fit = (
                f"planned prefix median {sel['planned_tokens_median']:,} tokens, max {sel['planned_tokens_max']:,} "
                f"over the {sel['fitting']} that fit"
                if sel["fitting"]
                else "none fits"
            )
            checked = (
                f", {sel['stop_checked']} of them checked at $0 to stop before any model call and counted at $0"
                if sel["would_stop"]
                else ""
            )
            p1, core = sel.get("p1_search_only_documents") or 0, sel.get("core_search_only_documents") or 0
            partial = (
                f"; {core} of them the AF or the contract, so {sel.get('partial', 0)} tenders get a partial review"
                if core
                else ""
            )
            say(
                f"prefix budget {sel['budget_tokens']:,} tokens: {sel['would_stop']} of {sel['planned']} tenders would "
                f"stop (fixed part and every document's outline over the budget{checked}); {fit}; "
                f"{sel['search_only_documents']} documents search-only"
                + (f", {p1} of them priority 1 read through search" if p1 else "")
                + partial
            )
    tiers = c.get("tiers") or {}
    if tiers:
        rows = []
        for engine, t in tiers.items():
            counts = ", ".join(f"{name} {n}" for name, n in t["counts"].items())
            above = f" ({t['above_top']} above the top tier)" if t["above_top"] else ""
            rows.append(f"{engine} {counts}{above}")
        say(f"hard-stop tiers, tenders per tier from the dry runs: {'; '.join(rows)}")
    parts = ", ".join(f"{k} ${v:.2f}" for k, v in c["estimate_usd"].items()) or "nothing to run"
    total = round(sum(c["estimate_usd"].values()), 2)
    unknown = (
        f"; {c['estimate_unknown']} engine estimates unknown, counted at their hard stop"
        if c["estimate_unknown"]
        else ""
    )
    say(
        f"estimate: ${total:.2f} ({parts}; {c['baseline_runs']} old-engine runs){unknown}; "
        f"worst case ${c['worst_usd']:.2f} (every hard stop)"
    )


def estimate_digest(report: Report) -> str | None:
    """A short hash of the estimate, so a paid run can say which estimate it was approved on. It hashes the one total
    over both splits (`combine`), never a split's own estimate: for a split of fewer than `MIN_GROUP` tenders that
    would be a hash of a figure for one tender, and comparing it across passes would show when that figure changed.
    None when the total covers fewer than `MIN_GROUP` tenders."""
    total = report.combined
    if int(total.get("tenders") or 0) < MIN_GROUP:
        return None
    payload = json.dumps({"estimate": total.get("estimate_usd"), "worst": total.get("worst_usd")}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]
