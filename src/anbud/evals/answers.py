"""The answer store: what really happened on a tender (the award, the opening protocol, Q&A, amendments, feedback),
kept by hash and only accepted once a prediction for that tender is frozen. That order is what makes a prediction
blind: the ledger shows it was hashed before any answer was in the system.

Layout, outside every repo:

    answers/<co>/<t>/files/<sha256><suffix>          dev tenders
    answers/<co>/<t>/index.jsonl                     one line per stored file, appended, fsynced
    sealed/answers/<co>/<t>/...                      held-out and prospective tenders; frozen in the sealed ledger

Intake keeps its own quarantine in `answers/<co>/<t>/intake/` and `intake.sha256`; this module never uses those names.
Messages and CLI output carry counts, kinds and short hashes, never a file name: a customer's file names are content.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anbud import paths
from anbud.evals import freeze
from anbud.evals.split_heldout import DEV, HELDOUT, PROSPECTIVE, status_of
from anbud.store import _fsync_dir, private_dirs

KINDS = frozenset({"award", "protocol", "qa", "amendment", "feedback", "other"})
MAX_BYTES = 1 << 30  # 1 GiB
INDEX = "index.jsonl"
FILES = "files"
LOCK = "answers.lock"
_SUFFIX = re.compile(r"^\.[a-z0-9]{1,8}$")


class AnswerError(RuntimeError):
    """An answer-store problem. Messages give a file's position in the list, never its name."""


@dataclass(frozen=True)
class AddResult:
    added: int
    skipped: int  # already stored (same hash), or repeated in this call
    sha256: tuple[str, ...]  # every file given, in order


@dataclass(frozen=True)
class AnswerStatus:
    split: str
    frozen: bool
    kinds: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.kinds.values())


@dataclass(frozen=True)
class _Where:
    folder: Path
    sealed: bool
    split: str


def _locate(company_id: str, tender_id: str, root: Path | None) -> _Where:
    """The split decides the store: dev answers beside the dev data, held-out and prospective ones in sealed storage."""
    try:
        paths.check_company_id(company_id)
        paths.check_tender_id(tender_id)
        split = status_of(company_id, tender_id, root)
    except ValueError as exc:  # bad ids, or a SplitError: both messages name no file
        raise AnswerError(str(exc)) from None
    except OSError as exc:
        raise AnswerError(f"{type(exc).__name__} while reading the split ledger") from None
    if split is None:
        raise AnswerError("this tender isn't in the split ledger")
    if split == DEV:
        return _Where(paths.answers_dir(company_id, tender_id, root), False, split)
    if split in (HELDOUT, PROSPECTIVE):
        return _Where((root or paths.data_root()) / "sealed" / "answers" / company_id / tender_id, True, split)
    raise AnswerError("unknown split status")


def _frozen(company_id: str, tender_id: str, where: _Where, root: Path | None) -> bool:
    try:
        return freeze.is_frozen(company_id, tender_id, sealed=where.sealed, root=root)
    except freeze.FreezeError as exc:
        raise AnswerError(f"the freeze ledger is unusable: {exc}") from None


def _check_store(folder: Path, root: Path | None) -> None:
    """No symlink anywhere from the data root down to the store's files: a redirected store could leak answers."""
    base = (root or paths.data_root()).expanduser()
    current = base
    for part in folder.relative_to(base).parts + (FILES,):
        current = current / part
        if current.is_symlink():
            raise AnswerError("a folder of the answer store is a symlink")
    for name in (INDEX, LOCK):
        if (folder / name).is_symlink():
            raise AnswerError("a file of the answer store is a symlink")


def _validate(files: list[Path], root: Path | None) -> list[Path]:
    """Every file checked before anything is written, so a bad one in the list adds nothing."""
    if not files:
        raise AnswerError("no files given")
    for number, path in enumerate(files, start=1):
        where = f"file {number} of {len(files)}"
        if path.is_symlink():
            raise AnswerError(f"{where}: a symlink; give the file itself")
        try:
            info = path.lstat()
        except OSError as exc:
            raise AnswerError(f"{where}: {type(exc).__name__}") from None
        if stat.S_ISDIR(info.st_mode):
            raise AnswerError(f"{where}: a folder; give its files")
        if not stat.S_ISREG(info.st_mode):
            raise AnswerError(f"{where}: not a regular file")
        if info.st_size > MAX_BYTES:
            raise AnswerError(f"{where}: over 1 GiB")
        if paths.is_sealed_path(path, root):
            raise AnswerError(f"{where}: inside held-out or sealed storage")
    return files


def suffix_of(path: Path) -> str:
    """The lowercased extension, if it looks like one; anything else (which could carry a name) is dropped."""
    suffix = path.suffix.lower()
    return suffix if _SUFFIX.fullmatch(suffix) else ""


def _read_index(folder: Path) -> list[dict[str, Any]]:
    index = folder / INDEX
    if not index.exists():
        return []
    fd = os.open(index, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        data = handle.read()
    entries = []
    for number, line in enumerate(data.splitlines(), start=1):
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str):
                raise ValueError
        except (ValueError, RecursionError):
            raise AnswerError(f"answer index line {number}: unreadable") from None
        entries.append(entry)
    return entries


@contextmanager
def _locked(folder: Path, root: Path | None) -> Iterator[None]:
    _check_store(folder, root)
    private_dirs(folder / FILES)
    fd = os.open(folder / LOCK, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _check_store(folder, root)
        yield
    finally:
        os.close(fd)


def _stage(source: Path, files_dir: Path, number: int, total: int) -> tuple[str, int, str]:
    """Copy one file to a temp name in the store, hashing what was copied (so the hash is of the stored bytes even if
    the source changes meanwhile). Returns the hash, the size and the temp path."""
    digest = hashlib.sha256()
    size = 0
    fd, tmp = tempfile.mkstemp(dir=files_dir, prefix=".tmp-")
    try:
        src = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(src, "rb") as reader, os.fdopen(fd, "wb") as writer:
            for chunk in iter(lambda: reader.read(1 << 20), b""):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise AnswerError(f"file {number} of {total}: over 1 GiB")
                digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        os.chmod(tmp, 0o600)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return digest.hexdigest(), size, tmp


def add(
    company_id: str,
    tender_id: str,
    files: Iterable[Path],
    kind: str,
    root: Path | None = None,
    now: datetime | None = None,
) -> AddResult:
    """Store answer files for a tender whose prediction is frozen. All or nothing: every file is checked and copied
    to a temp name before any is renamed into place or indexed."""
    if kind not in KINDS:
        raise AnswerError(f"kind must be one of {', '.join(sorted(KINDS))}")
    where = _locate(company_id, tender_id, root)
    if not _frozen(company_id, tender_id, where, root):
        raise AnswerError("no frozen prediction for this tender; freeze a run before adding answers")
    sources = _validate([Path(f) for f in files], root)
    stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
    files_dir = where.folder / FILES
    with _locked(where.folder, root):
        known = {e["sha256"] for e in _read_index(where.folder)}
        staged: list[tuple[str, int, str, Path]] = []
        try:
            for number, source in enumerate(sources, start=1):
                sha, size, tmp = _stage(source, files_dir, number, len(sources))
                staged.append((sha, size, tmp, source))
            lines: list[bytes] = []
            hashes: list[str] = []
            skipped = 0
            for sha, size, tmp, source in staged:
                hashes.append(sha)
                if sha in known:
                    skipped += 1
                    continue
                known.add(sha)
                suffix = suffix_of(source)
                os.replace(tmp, files_dir / f"{sha}{suffix}")
                entry = {
                    "v": 1,
                    "sha256": sha,
                    "kind": kind,
                    "suffix": suffix,
                    "size": size,
                    "added_at": stamp,
                    "name": source.name,
                }
                lines.append(json.dumps(entry, ensure_ascii=False, sort_keys=True).encode() + b"\n")
        finally:
            for _, _, tmp, _ in staged:
                Path(tmp).unlink(missing_ok=True)  # duplicates, or everything after a failure
        if lines:
            _fsync_dir(files_dir)
            fd = os.open(where.folder / INDEX, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                payload = b"".join(lines)
                if os.write(fd, payload) != len(payload):
                    raise AnswerError("a short write to the answer index")
                os.fsync(fd)
            finally:
                os.close(fd)
    return AddResult(len(lines), skipped, tuple(hashes))


def status(company_id: str, tender_id: str, root: Path | None = None) -> AnswerStatus:
    where = _locate(company_id, tender_id, root)
    frozen = _frozen(company_id, tender_id, where, root)
    _check_store(where.folder, root)
    kinds = Counter(str(e.get("kind")) for e in _read_index(where.folder))
    return AnswerStatus(where.split, frozen, dict(sorted(kinds.items())))
