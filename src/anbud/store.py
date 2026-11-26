"""The run store: content-addressed blobs, an append-only event log per run, and the step cache.

One mechanism gives resume after a crash, free replay of code-only changes, and the data for evals. Layout, under
`~/estim-anbud-data/<company_id>/`, outside every repo and never in held-out or sealed storage:

    blobs/<aa>/<sha256>          request and response bodies, by the hash of their bytes
    steps/<aa>/<key>.json        step cache: a request key -> its response blob and cost
    runs/<run_id>/run.json       the run's salt and the code it started with, written once
    runs/<run_id>/events.jsonl   one Event per line, appended, never rewritten
    runs/<run_id>/STOP           `anbud stop` marker for one run
and `~/estim-anbud-data/STOP` stops every run.

- A step key is the hash of (run salt, company id, step, sample number, the exact request). The request holds the
  model, effort, tools, system blocks and messages, so it covers the prompt, the model, the effort, the tool list and
  every input. The same salt replays or resumes a run; a new salt samples fresh.
- Every file is written to a temporary name and renamed, so a crash never leaves half a blob or half a cache entry.
  Events are appended one whole line per write.
- Blobs hold tender text and model output. They stay here, with the customer's data. Events hold ids, sizes, timings
  and costs only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Any

from anbud.models import Event
from anbud.paths import company_dir, data_root, is_sealed_path

RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


class StoreError(RuntimeError):
    pass


def _jsonable(value: Any) -> Any:
    """SDK objects (content blocks passed back in a tool loop) as the JSON the SDK sends for them."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    raise TypeError(f"can't store a {type(value).__name__}")


def canonical(value: Any) -> bytes:
    """The one byte form used for hashing and storing JSON."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_jsonable).encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_run_id(run_id: str) -> str:
    if not RUN_ID.fullmatch(run_id):
        raise StoreError("run ids are letters, digits, dot, dash and underscore, at most 81 characters")
    return run_id


class RunStore:
    def __init__(self, company_id: str, root: Path | None = None) -> None:
        self.company_id = company_id
        self.root = (root or data_root()).expanduser()
        self.dir = company_dir(company_id, self.root)
        if is_sealed_path(self.dir, self.root):
            raise StoreError("the run store never lives in held-out or sealed storage")
        private_dirs(self.dir)
        self._lock = threading.Lock()

    # Files

    def _temp(self, path: Path, data: bytes) -> str:
        private_dirs(path.parent)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return tmp

    def _write_atomic(self, path: Path, data: bytes) -> None:
        tmp = self._temp(path, data)
        try:
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        _fsync_dir(path.parent)  # the rename itself survives a power loss

    def write_file(self, path: Path, data: bytes) -> None:
        """Write a file inside this company's folder, atomically and private to the user."""
        resolved = path.resolve()
        if not resolved.is_relative_to(self.dir.resolve()):
            raise StoreError("a write outside the company's folder")
        self._write_atomic(path, data)

    # Blobs

    def _blob_path(self, sha: str) -> Path:
        if not _SHA.fullmatch(sha):
            raise StoreError("not a sha256")
        return self.dir / "blobs" / sha[:2] / sha

    def put_blob(self, data: bytes) -> str:
        sha = _sha(data)
        path = self._blob_path(sha)
        if not path.exists():
            self._write_atomic(path, data)
        return sha

    def get_blob(self, sha: str) -> bytes:
        data = self._blob_path(sha).read_bytes()
        if _sha(data) != sha:
            raise StoreError("a blob doesn't match its hash")
        return data

    def put_json(self, value: Any) -> str:
        return self.put_blob(canonical(value))

    # Step cache

    def step_key(self, salt: str, request: dict[str, Any], step: str = "", sample: int = 0) -> str:
        """Two identical requests in one run are the same call unless their step or sample number differs: a
        verifier's second vote passes `sample=1` to get a fresh answer."""
        ident = {"salt": salt, "company_id": self.company_id, "step": step, "sample": sample, "request": request}
        return _sha(canonical(ident))

    def _step_path(self, key: str) -> Path:
        if not _SHA.fullmatch(key):
            raise StoreError("not a step key")
        return self.dir / "steps" / key[:2] / f"{key}.json"

    def step_get(self, key: str) -> dict[str, Any] | None:
        path = self._step_path(key)
        if not path.exists():
            return None
        try:
            record = json.loads(path.read_text())
            self.get_blob(record["request"])
            self.get_blob(record["response"])  # an entry is only good if both bodies are intact
        except (OSError, ValueError, KeyError, TypeError, StoreError):
            return None  # treated as a miss: the call is made again and the entry rewritten
        return record

    def step_put(self, key: str, record: dict[str, Any]) -> None:
        self._write_atomic(self._step_path(key), canonical(record))

    # Runs

    def run_dir(self, run_id: str) -> Path:
        return self.dir / "runs" / check_run_id(run_id)

    def open_run(
        self, run_id: str, salt: str | None = None, code: dict[str, Any] | None = None, base_run: str | None = None
    ) -> str:
        """The run's salt: read back for an existing run (resume, replay), created for a new one. `code`, the code the
        run started with, and `base_run`, the run a rebuild took its salt from, go into a new run's manifest; an
        existing manifest is never rewritten, so a resumed rebuild keeps the base it started from."""
        manifest = self.run_dir(run_id) / "run.json"
        if not manifest.exists():
            chosen = salt or secrets.token_hex(16)
            record: dict[str, Any] = {"salt": chosen, "company_id": self.company_id}
            if code is not None:
                record["code"] = code
            if base_run is not None:
                record["base_run"] = check_run_id(base_run)
            tmp = self._temp(manifest, canonical(record))
            try:
                os.link(tmp, manifest)  # fails if another process created the run first; then its salt wins
                _fsync_dir(manifest.parent)
            except FileExistsError:
                pass
            finally:
                Path(tmp).unlink(missing_ok=True)
        stored = json.loads(manifest.read_text())["salt"]
        if salt is not None and salt != stored:
            raise StoreError("this run already has a different salt")
        return stored

    def stored_base_run(self, run_id: str) -> str | None:
        """The run a stored rebuild took its salt from; None for a run that isn't a rebuild or doesn't exist."""
        manifest = self.run_dir(run_id) / "run.json"
        if not manifest.is_file():
            return None
        base = json.loads(manifest.read_text()).get("base_run")
        return str(base) if base is not None else None

    def stored_salt(self, run_id: str) -> str | None:
        """A stored run's salt, read without writing anything to it (a frozen run is read-only); None if there's no
        such run."""
        manifest = self.run_dir(run_id) / "run.json"
        if not manifest.is_file():
            return None
        return str(json.loads(manifest.read_text())["salt"])

    def append_event(self, event: Event) -> None:
        if event.company_id != self.company_id:
            raise StoreError("an event for another company")
        path = self.run_dir(event.run_id) / "events.jsonl"
        private_dirs(path.parent)
        line = (event.model_dump_json() + "\n").encode()
        with self._lock:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)

    def run_spend(self, run_id: str) -> float:
        """What the run has already spent, from its events: resumed runs keep counting against the same budget."""
        return sum(e.cost_usd or 0.0 for e in self.events(run_id) if e.kind in ("model_call", "error"))

    def events(self, run_id: str) -> list[Event]:
        path = self.run_dir(run_id) / "events.jsonl"
        if not path.exists():
            return []
        return [Event.model_validate_json(line) for line in path.read_text().splitlines() if line]

    # Stop markers

    def stop_requested(self, run_id: str) -> bool:
        return (self.root / "STOP").exists() or (self.run_dir(run_id) / "STOP").exists()


def request_stop(company_id: str | None, run_id: str | None, root: Path | None = None, clear: bool = False) -> Path:
    """Write (or with `clear`, remove) a stop marker for one run, or for every run when no run is given."""
    base = (root or data_root()).expanduser()
    if run_id is None:
        marker = base / "STOP"
    else:
        if company_id is None:
            raise StoreError("a run's stop marker needs its company id")
        marker = company_dir(company_id, base) / "runs" / check_run_id(run_id) / "STOP"
    if clear:
        marker.unlink(missing_ok=True)
    else:
        private_dirs(marker.parent)
        marker.touch(mode=0o600)
    return marker


def private_dirs(path: Path) -> None:
    """Create every missing folder down to `path` as 0o700 (mkdir's mode only applies to the last one)."""
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for folder in reversed(missing):
        folder.mkdir(mode=0o700, exist_ok=True)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
