"""The run store: content-addressed blobs, an append-only event log per run, and the step cache.

One mechanism gives resume after a crash, free replay of code-only changes, and the data for evals. Layout, under
`~/estim-anbud-data/<company_id>/`, outside every repo and never in held-out or sealed storage:

    blobs/<aa>/<sha256>          request and response bodies, by the hash of their bytes
    steps/<aa>/<key>.json        step cache: a request key -> its response blob and cost
    runs/<run_id>/run.json       the run's salt, written once
    runs/<run_id>/events.jsonl   one Event per line, appended, never rewritten
    runs/<run_id>/STOP           `anbud stop` marker for one run
and `~/estim-anbud-data/STOP` stops every run.

- A step key is the hash of (run salt, company id, the exact request). The request holds the model, effort, tools,
  system blocks and messages, so it covers the prompt, the model, the effort, the tool list and every input. The same
  salt replays or resumes a run; a new salt samples fresh.
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


def canonical(value: Any) -> bytes:
    """The one byte form used for hashing and storing JSON."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


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
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.Lock()

    # Files

    def _write_atomic(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

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

    def step_key(self, salt: str, request: dict[str, Any]) -> str:
        return _sha(canonical({"salt": salt, "company_id": self.company_id, "request": request}))

    def _step_path(self, key: str) -> Path:
        if not _SHA.fullmatch(key):
            raise StoreError("not a step key")
        return self.dir / "steps" / key[:2] / f"{key}.json"

    def step_get(self, key: str) -> dict[str, Any] | None:
        path = self._step_path(key)
        if not path.exists():
            return None
        record = json.loads(path.read_text())
        self.get_blob(record["response"])  # a cache entry is only good if its response is intact
        return record

    def step_put(self, key: str, record: dict[str, Any]) -> None:
        self._write_atomic(self._step_path(key), canonical(record))

    # Runs

    def run_dir(self, run_id: str) -> Path:
        return self.dir / "runs" / check_run_id(run_id)

    def open_run(self, run_id: str, salt: str | None = None) -> str:
        """The run's salt: read back for an existing run (resume, replay), created for a new one."""
        manifest = self.run_dir(run_id) / "run.json"
        if manifest.exists():
            stored = json.loads(manifest.read_text())["salt"]
            if salt is not None and salt != stored:
                raise StoreError("this run already has a different salt")
            return stored
        chosen = salt or secrets.token_hex(16)
        self._write_atomic(manifest, canonical({"salt": chosen, "company_id": self.company_id}))
        return chosen

    def append_event(self, event: Event) -> None:
        if event.company_id != self.company_id:
            raise StoreError("an event for another company")
        path = self.run_dir(event.run_id) / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        line = (event.model_dump_json() + "\n").encode()
        with self._lock:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)

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
        marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker.touch(mode=0o600)
    return marker
