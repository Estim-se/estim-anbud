"""The freeze ledger: every run's inputs, code version and outputs, hashed into a chained, append-only file before any
answer for its tender can enter the system, so a blind prediction can be proven blind afterwards.

Layout, outside every repo:

    predictions/<company_id>/ledger.jsonl          dev runs
    sealed/predictions/<company_id>/ledger.jsonl   the held-out exam's runs

- Each line is one JSON object, newline-terminated. Every line after the first carries "prev": the sha256 of the
  previous line's exact bytes (without its newline), so editing, dropping or reordering any earlier line breaks the
  chain. New lines are `store.canonical` bytes; imported hand-made lines keep their own bytes.
- Lines are appended under an exclusive lock, after re-verifying the chain, in one write followed by fsync. Nothing
  ever rewrites or reorders a line.
- A frozen run's output files become read-only (0o400).
- Records hold ids, hashes, timestamps and data-root-relative paths of the harness's own folders, never document text,
  model output or a customer's file name. A folder other than the run's own (`freeze run --outputs`), and any file
  name the harness wouldn't write, is stored as "sha256:" and the hash of its relative path. Errors name a line
  number or a reason, never a file.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anbud import paths
from anbud.store import StoreError, canonical, check_run_id, private_dirs

LEDGER = "ledger.jsonl"
LOCK = "ledger.lock"
FREEZE_KINDS = ("freeze", "manual_freeze_v0")
PLAN_KIND = "preregistration"  # a scoring plan, frozen before any paid exam
HASHED_REPO_DIRS = ("config", "schemas", "lenses")
PREP_FILES = ("blocks.jsonl", "documents.json", "summary.json")
# The names the harness itself gives files and folders in a run; anything else may be a customer's name.
_HARNESS_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
# The key in freeze_run's result (never in the ledger) that lists outputs left writable.
NOT_READ_ONLY = "not_read_only"
# Fields that may differ when the same run is frozen again and still count as the same freeze.
_VOLATILE = frozenset({"frozen_at", "prev"})


class FreezeError(RuntimeError):
    """A ledger or freeze problem. Messages name a line number or a reason, never a file or its content."""


@dataclass(frozen=True)
class LedgerSummary:
    lines: int
    last_sha256: str | None
    kinds: dict[str, int]
    tenders: tuple[str, ...]  # "<co>/<t>" with a freeze, sorted


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _company(company_id: str) -> str:
    try:
        return paths.check_company_id(company_id)
    except ValueError as exc:
        raise FreezeError(str(exc)) from None


def _tender(tender_id: str) -> str:
    try:
        return paths.check_tender_id(tender_id)
    except ValueError as exc:
        raise FreezeError(str(exc)) from None


def ledger_file(company_id: str, sealed: bool = False, root: Path | None = None) -> Path:
    base = root or paths.data_root()
    if sealed:
        return base / "sealed" / "predictions" / _company(company_id) / LEDGER
    return paths.predictions_dir(_company(company_id), base) / LEDGER


def _check_no_symlinks(ledger: Path, root: Path | None) -> None:
    """Refuse a ledger that is a symlink, dangling or not (is_symlink before exists), or any folder between the data
    root and it that is one: a redirected ledger could be swapped for another chain."""
    base = (root or paths.data_root()).expanduser()
    if ledger.is_symlink():
        raise FreezeError("the ledger is a symlink; it must be a plain file")
    try:
        below = ledger.parent.relative_to(base)
    except ValueError:
        raise FreezeError("the ledger is outside the data root") from None
    current = base
    for part in below.parts:
        current = current / part
        if current.is_symlink():
            raise FreezeError("a folder above the ledger is a symlink")
    if ledger.exists() and not ledger.is_file():
        raise FreezeError("the ledger is not a regular file")


def _parse(data: bytes) -> tuple[list[bytes], list[dict[str, Any]]]:
    """The lines (without newlines) and their objects, after checking the chain."""
    if not data:
        return [], []
    if not data.endswith(b"\n"):
        number = data.count(b"\n") + 1
        raise FreezeError(f"ledger line {number}: no trailing newline (a cut-off write)")
    lines = data[:-1].split(b"\n")
    records: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            raise FreezeError(f"ledger line {number}: blank")
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            raise FreezeError(f"ledger line {number}: not JSON") from None
        if not isinstance(record, dict):
            raise FreezeError(f"ledger line {number}: not a JSON object")
        if number == 1:
            if "prev" in record:
                raise FreezeError("ledger line 1: the first line has a prev")
        elif record.get("prev") != _sha(lines[number - 2]):
            raise FreezeError(f"ledger line {number}: prev doesn't match line {number - 1}")
        records.append(record)
    return lines, records


def _summary(lines: list[bytes], records: list[dict[str, Any]]) -> LedgerSummary:
    kinds = Counter(str(r.get("kind")) for r in records)
    tenders = {r["tender"] for r in records if r.get("kind") in FREEZE_KINDS and isinstance(r.get("tender"), str)}
    last = _sha(lines[-1]) if lines else None
    return LedgerSummary(len(lines), last, dict(sorted(kinds.items())), tuple(sorted(tenders)))


def _read(path: Path, root: Path | None = None) -> tuple[list[bytes], list[dict[str, Any]]]:
    """The checks a write makes apply to every read too: the answer store opens on what a read finds."""
    _check_no_symlinks(path, root)
    if not path.exists():
        return [], []
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        return _parse(handle.read())


def verify(path: Path, root: Path | None = None) -> LedgerSummary:
    """Check a ledger's chain end to end. A missing ledger is an empty one."""
    return _summary(*_read(path, root))


def freezes_by_tender(path: Path, root: Path | None = None) -> Counter[str]:
    """How many frozen runs each tender ("<co>/<t>") has, after the same checks as `verify`."""
    _, records = _read(path, root)
    return Counter(r["tender"] for r in records if r.get("kind") in FREEZE_KINDS and isinstance(r.get("tender"), str))


@contextmanager
def _locked(ledger: Path, root: Path | None) -> Iterator[None]:
    """One writer at a time, on a sibling lock file; released when the file closes."""
    _check_no_symlinks(ledger, root)
    private_dirs(ledger.parent)
    fd = os.open(ledger.parent / LOCK, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _check_no_symlinks(ledger, root)  # again, under the lock
        yield
    finally:
        os.close(fd)


def _append(ledger: Path, payload: bytes) -> None:
    """One write and fsync, so a crash leaves either the whole line or a cut-off last line that verify refuses."""
    fd = os.open(ledger, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        written = os.write(fd, payload)
        if written != len(payload):
            raise FreezeError("a short write to the ledger; check its last line before the next freeze")
        os.fsync(fd)
    finally:
        os.close(fd)


def import_manual(company_id: str, source: Path, root: Path | None = None) -> int:
    """Put hand-made, already chained freeze records at the start of the ledger, byte for byte. Returns the number of
    lines written: 0 when the ledger already starts with exactly those lines. When the ledger holds an earlier
    import of the same file and nothing else, and the file has grown since, the new lines are appended (their chain
    continues from the ledger's last line). Any other existing ledger is refused, because the hand-made records must
    come first and lines are never reordered."""
    ledger = ledger_file(company_id, root=root)
    if paths.is_sealed_path(source, root):
        raise FreezeError("the hand-made records may not come from held-out or sealed storage")
    if source.is_symlink() or not source.is_file():
        raise FreezeError("the hand-made records must be a plain file")
    data = source.read_bytes()
    try:
        lines, _ = _parse(data)
    except FreezeError as exc:
        raise FreezeError(f"the hand-made records: {exc}") from None
    if not lines:
        raise FreezeError("the hand-made records are empty")
    with _locked(ledger, root):
        existing, _ = _read(ledger, root)
        if not existing:
            _append(ledger, data)
            return len(lines)
        if existing[: len(lines)] == lines:
            return 0
        if len(existing) < len(lines) and lines[: len(existing)] == existing:
            _append(ledger, b"".join(line + b"\n" for line in lines[len(existing) :]))
            return len(lines) - len(existing)
        raise FreezeError("the ledger already holds other lines; hand-made records can only start a ledger")


def is_frozen(company_id: str, tender_id: str, sealed: bool = False, root: Path | None = None) -> bool:
    key = f"{_company(company_id)}/{_tender(tender_id)}"
    _, records = _read(ledger_file(company_id, sealed, root), root)
    return any(r.get("tender") == key and r.get("kind") in FREEZE_KINDS for r in records)


def frozen_line(company_id: str, run_id: str, sealed: bool = False, root: Path | None = None) -> int | None:
    """The ledger line that froze this run, or None. A frozen run's outputs and record never change, so a paid run
    must not resume it."""
    try:
        check_run_id(run_id)
    except StoreError as exc:
        raise FreezeError(str(exc)) from None
    _, records = _read(ledger_file(company_id, sealed, root), root)
    for number, record in enumerate(records, start=1):
        if record.get("kind") in FREEZE_KINDS and record.get("run_id") == run_id:
            return number
    return None


# Hashing


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree(folder: Path, what: str) -> dict[str, Path]:
    """Every regular file under the folder by posix relative path, sorted. A symlink anywhere, or a special file, is
    refused: it could point the hash at something other than what the run wrote. So is a folder the walk can't list,
    which os.walk would otherwise skip in silence and leave out of the hashes."""

    def unreadable(exc: OSError) -> None:
        raise FreezeError(f"a folder in the {what} couldn't be read ({type(exc).__name__})")

    found: dict[str, Path] = {}
    for current, dirs, files in os.walk(folder, onerror=unreadable, followlinks=False):
        here = Path(current)
        for name in sorted(dirs + files):
            entry = here / name
            mode = entry.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise FreezeError(f"the {what} hold a symlink")
            if stat.S_ISREG(mode):
                found[entry.relative_to(folder).as_posix()] = entry
            elif not stat.S_ISDIR(mode):
                raise FreezeError(f"the {what} hold a file that isn't a regular file")
    return dict(sorted(found.items()))


def _named(rel: str, own: bool) -> str:
    """A relative path as the ledger may hold it: in clear inside the harness's own folder when every part is a name
    the harness writes, otherwise as the hash of the path, which a later check can still recompute."""
    if own and all(_HARNESS_NAME.fullmatch(part) for part in rel.split("/")):
        return rel
    return "sha256:" + _sha(rel.encode())


def _hash_tree(files: dict[str, Path], own: bool) -> dict[str, str]:
    return dict(sorted((_named(rel, own), _file_sha(path)) for rel, path in files.items()))


def repo_hashes(repo: Path | None = None) -> dict[str, str]:
    """Every file under config/, schemas/ and lenses/, by repo-relative path. Hidden files (.DS_Store) are left out:
    they aren't part of the harness and would change the hash without changing a run."""
    repo = repo or paths.repo_root()
    out: dict[str, str] = {}
    for name in HASHED_REPO_DIRS:
        folder = repo / name
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            rel = path.relative_to(repo)
            if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
                continue
            if path.is_file() and not path.is_symlink():
                out[rel.as_posix()] = _file_sha(path)
    return dict(sorted(out.items()))


def _check_location(folder: Path, company_id: str, sealed: bool, root: Path | None, what: str) -> Path:
    """The folder, resolved, if it is a real folder inside the company's data (or, for the exam, sealed storage)."""
    if folder.is_symlink():
        raise FreezeError(f"the {what} folder is a symlink")
    if not folder.is_dir():
        raise FreezeError(f"the {what} folder doesn't exist")
    resolved = folder.resolve()
    in_sealed = paths.is_sealed_path(resolved, root)
    if in_sealed and not sealed:
        raise FreezeError(f"the {what} are in held-out or sealed storage; a dev freeze never reads there")
    baselines = paths.predictions_dir(company_id, root).resolve() / "baseline"
    inside = resolved.is_relative_to(paths.company_dir(company_id, root).resolve()) or resolved.is_relative_to(
        baselines
    )
    if not inside and not (sealed and in_sealed):
        raise FreezeError(f"the {what} are outside the company's folder")
    return resolved


def _relative(path: Path, root: Path | None) -> str:
    return path.relative_to((root or paths.data_root()).expanduser().resolve()).as_posix()


def _is(path: Path, expected: Path) -> bool:
    """The resolved folder is the harness's own folder for this run (its path is built from ids only)."""
    return path == expected.expanduser().resolve()


def _harness(run_record_dir: Path) -> dict[str, Any]:
    """The code the run started with, from its manifest; the current code only for a run that recorded none."""
    manifest = run_record_dir / "run.json"
    if manifest.is_file() and not manifest.is_symlink():
        try:
            code = json.loads(manifest.read_bytes()).get("code")
        except (ValueError, AttributeError):
            raise FreezeError("the run's manifest is unreadable") from None
        if isinstance(code, dict):
            return code
    from anbud.granskning import provenance

    return provenance.code_block(provenance.git_state())


def _inputs(
    company_id: str, tender_id: str | None, root: Path | None, input_dir: Path | None = None
) -> tuple[str | None, dict[str, str]]:
    if tender_id is None:
        return None, {}
    tender = input_dir if input_dir is not None else paths.dev_tender_dir(company_id, tender_id, root)
    manifest = tender / "inbox.sha256"
    manifest_sha = _file_sha(manifest) if manifest.is_file() else None
    prep = {name: _file_sha(tender / "prep" / name) for name in PREP_FILES if (tender / "prep" / name).is_file()}
    return manifest_sha, prep


def _own(
    company_id: str, tender_id: str | None, run_id: str, outputs: Path, record_dir: Path, root: Path | None
) -> tuple[bool, bool]:
    """Whether the outputs and the run record are in the harness's own folders for this run (paths built from ids
    only), so the ledger may name them in clear: a Granskning run's, or an old-engine baseline run's."""
    own_outputs = own_record = False
    if tender_id is not None:
        baseline = paths.baseline_dir(company_id, tender_id, root) / run_id
        own_outputs = _is(outputs, paths.dev_tender_dir(company_id, tender_id, root) / "runs" / run_id) or _is(
            outputs, baseline / "outputs"
        )
        own_record = _is(record_dir, baseline / "record")
    own_record = own_record or _is(record_dir, paths.company_dir(company_id, root) / "runs" / run_id)
    return own_outputs, own_record


def _same_freeze(old: dict[str, Any], new: dict[str, Any]) -> bool:
    keys = (set(old) | set(new)) - _VOLATILE
    return all(old.get(k) == new.get(k) for k in keys)


def freeze_run(
    company_id: str,
    run_id: str,
    *,
    outputs_dir: Path,
    run_record_dir: Path,
    tender_id: str | None,
    sealed: bool = False,
    now: datetime | None = None,
    root: Path | None = None,
    input_dir: Path | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Hash a finished run into the ledger and make its outputs read-only. Returns the record with its
    `line_number` and `line_sha256`. Freezing a run again is a no-op when every hash still matches, and refused
    otherwise: a frozen prediction never changes.

    The exam passes `input_dir`, the tender folder the run read (its inbox manifest and prep are hashed from there
    rather than from the dev folder), and `extra`, fields of its own such as the input variant."""
    _company(company_id)
    try:
        check_run_id(run_id)
    except StoreError as exc:
        raise FreezeError(str(exc)) from None
    if tender_id is not None:
        _tender(tender_id)
    outputs = _check_location(outputs_dir, company_id, sealed, root, "outputs")
    record_dir = _check_location(run_record_dir, company_id, sealed, root, "run record")
    output_files = _tree(outputs, "outputs")
    if not output_files:
        raise FreezeError("the outputs folder has no files")
    record_files = _tree(record_dir, "run record")
    own_outputs, own_record = _own(company_id, tender_id, run_id, outputs, record_dir, root)

    config = repo_hashes()
    manifest_sha, prep = _inputs(company_id, tender_id, root, input_dir)
    record: dict[str, Any] = {
        "v": 1,
        "kind": "freeze",
        "tender": f"{company_id}/{tender_id}" if tender_id else None,
        "run_id": run_id,
        "frozen_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds"),
        "harness": _harness(record_dir),
        "config_sha256": config,
        "config_set_sha256": _sha(canonical(config)),
        "input_manifest_sha256": manifest_sha,
        "prep_sha256": prep,
        "outputs_dir": _relative(outputs, root) if own_outputs else _named(_relative(outputs, root), own=False),
        "run_record_dir": _relative(record_dir, root) if own_record else _named(_relative(record_dir, root), own=False),
        "outputs_sha256": _hash_tree(output_files, own_outputs),
        "run_record_sha256": _hash_tree(record_files, own_record),
    }
    for key, value in (extra or {}).items():
        if key in record:
            raise FreezeError(f"an extra field would replace the record's own {key}")
        record[key] = value

    ledger = ledger_file(company_id, sealed, root)
    with _locked(ledger, root):
        lines, records = _read(ledger, root)
        for number, old in enumerate(records, start=1):
            if old.get("kind") == "freeze" and old.get("run_id") == run_id:
                if not _same_freeze(old, record):
                    raise FreezeError(f"run already frozen at ledger line {number} with different hashes")
                failed = _read_only(output_files)
                return {**old, "line_number": number, "line_sha256": _sha(lines[number - 1]), NOT_READ_ONLY: failed}
        if lines:
            record["prev"] = _sha(lines[-1])
        line = canonical(record)
        _append(ledger, line + b"\n")
    # The line is written, so the run is frozen whatever happens next; a failure here is reported, never raised.
    failed = _read_only(output_files)
    return {**record, "line_number": len(lines) + 1, "line_sha256": _sha(line), NOT_READ_ONLY: failed}


def relocate_run(
    company_id: str,
    run_id: str,
    *,
    outputs_dir: Path,
    run_record_dir: Path,
    sealed: bool = False,
    now: datetime | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Record that a frozen run's folders moved. Every file at the new place must hash as it did when frozen (same
    files, same contents); then one `relocate` line names the new place. Nothing is moved here: the caller moves
    the folders first. Returns the record with its `line_number`."""
    _company(company_id)
    try:
        check_run_id(run_id)
    except StoreError as exc:
        raise FreezeError(str(exc)) from None
    outputs = _check_location(outputs_dir, company_id, sealed, root, "outputs")
    record_dir = _check_location(run_record_dir, company_id, sealed, root, "run record")
    output_files = _tree(outputs, "outputs")
    record_files = _tree(record_dir, "run record")
    ledger = ledger_file(company_id, sealed, root)
    with _locked(ledger, root):
        lines, records = _read(ledger, root)
        found = [
            (n, r) for n, r in enumerate(records, start=1) if r.get("kind") == "freeze" and r.get("run_id") == run_id
        ]
        if not found:
            raise FreezeError("no frozen run has this id")
        number, frozen = found[-1]
        tender = frozen.get("tender")
        tender_id = tender.split("/", 1)[1] if isinstance(tender, str) and "/" in tender else None
        checks = ((output_files, "outputs_sha256", "outputs"), (record_files, "run_record_sha256", "run record"))
        for files, field, what in checks:
            # A run outside the harness's own folders was frozen with hashed names: accept either naming.
            if frozen.get(field) not in (_hash_tree(files, own=False), _hash_tree(files, own=True)):
                raise FreezeError(f"the {what} at the new place don't match the freeze at ledger line {number}")
        own_outputs, own_record = _own(company_id, tender_id, run_id, outputs, record_dir, root)
        record: dict[str, Any] = {
            "v": 1,
            "kind": "relocate",
            "tender": tender,
            "run_id": run_id,
            "frozen_line": number,
            "relocated_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds"),
            "outputs_dir": _relative(outputs, root) if own_outputs else _named(_relative(outputs, root), own=False),
            "run_record_dir": _relative(record_dir, root)
            if own_record
            else _named(_relative(record_dir, root), own=False),
            "outputs_sha256": _hash_tree(output_files, own_outputs),
            "run_record_sha256": _hash_tree(record_files, own_record),
            "prev": _sha(lines[-1]),
        }
        line = canonical(record)
        _append(ledger, line + b"\n")
    return {**record, "line_number": len(lines) + 1, "line_sha256": _sha(line)}


def register_plan(
    company_id: str, plan_file: Path, *, now: datetime | None = None, root: Path | None = None
) -> dict[str, Any]:
    """Pre-register a scoring plan: one chained `preregistration` line in `predictions/<co>/ledger.jsonl` with the
    file's name and sha256. The plan must be a plain file directly in `predictions/<co>/`, named in lower case. The
    same bytes registered again are a no-op; changed bytes are a new version with a line of their own. Returns the
    record with its `line_number` and `new`."""
    _company(company_id)
    folder = paths.predictions_dir(company_id, root)
    if plan_file.is_symlink() or not plan_file.is_file():
        raise FreezeError("the scoring plan isn't a regular file")
    resolved = plan_file.resolve()
    if resolved.parent != folder.resolve() or folder.is_symlink():
        raise FreezeError("the scoring plan must be a file directly in predictions/<co>/")
    if not _HARNESS_NAME.fullmatch(resolved.name):
        raise FreezeError("name the scoring plan in lower-case letters, digits, dots and dashes")
    sha = _file_sha(resolved)
    ledger = ledger_file(company_id, False, root)
    with _locked(ledger, root):
        lines, records = _read(ledger, root)
        plans = [(n, r) for n, r in enumerate(records, start=1) if r.get("kind") == PLAN_KIND]
        if plans and plans[-1][1].get("sha256") == sha and plans[-1][1].get("file") == resolved.name:
            return {**plans[-1][1], "line_number": plans[-1][0], "new": False}
        record: dict[str, Any] = {
            "v": 1,
            "kind": PLAN_KIND,
            "company": company_id,
            "file": resolved.name,
            "sha256": sha,
            "registered_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds"),
        }
        if lines:
            record["prev"] = _sha(lines[-1])
        _append(ledger, canonical(record) + b"\n")
    return {**record, "line_number": len(lines) + 1, "new": True}


def current_plan(company_id: str, root: Path | None = None) -> dict[str, Any] | None:
    """The latest registered scoring plan, with its `line_number`, or None when none is registered. Raises when its
    file no longer holds the registered bytes: a changed plan is registered again before it counts."""
    _, records = _read(ledger_file(_company(company_id), False, root), root)
    plans = [(n, r) for n, r in enumerate(records, start=1) if r.get("kind") == PLAN_KIND]
    if not plans:
        return None
    number, record = plans[-1]
    path = paths.predictions_dir(company_id, root) / str(record.get("file"))
    if path.is_symlink() or not path.is_file() or _file_sha(path) != record.get("sha256"):
        raise FreezeError(f"the scoring plan registered at ledger line {number} changed or is gone; register it again")
    return {**record, "line_number": number}


def _read_only(files: dict[str, Path]) -> list[str]:
    """Make every file 0o400. Returns the error type for each file that couldn't be, so the caller can say so."""
    failed: list[str] = []
    for path in files.values():
        try:
            os.chmod(path, 0o400, follow_symlinks=False)
        except OSError as exc:
            failed.append(type(exc).__name__)
    return failed


def read_only_warning(frozen: dict[str, Any]) -> str | None:
    """What to tell the operator when a frozen run's outputs aren't all read-only, or None when they are."""
    failed = frozen.get(NOT_READ_ONLY) or []
    if not failed:
        return None
    return f"frozen, but {len(failed)} output files could not be made read-only ({', '.join(sorted(set(failed)))})"


def find_run_outputs(company_id: str, run_id: str, root: Path | None = None) -> tuple[Path, str]:
    """A Granskning run's outputs, `<co>/dev/<t>/runs/<run_id>/`, and its tender id. Exactly one must match."""
    try:
        check_run_id(run_id)
    except StoreError as exc:
        raise FreezeError(str(exc)) from None
    dev = paths.company_dir(_company(company_id), root) / "dev"
    matches = sorted(
        p for p in dev.glob(f"t-*/runs/{run_id}") if p.is_dir() and paths.TENDER_ID.fullmatch(p.parent.parent.name)
    )
    if not matches:
        raise FreezeError("no dev tender has a run with this id; pass --outputs")
    if len(matches) > 1:
        raise FreezeError(f"{len(matches)} dev tenders have a run with this id; pass --tender")
    return matches[0], matches[0].parent.parent.name


def run_record_dir(company_id: str, run_id: str, root: Path | None = None) -> Path:
    try:
        check_run_id(run_id)
    except StoreError as exc:
        raise FreezeError(str(exc)) from None
    return paths.company_dir(_company(company_id), root) / "runs" / run_id
