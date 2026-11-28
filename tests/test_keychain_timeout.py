"""The Keychain read fails fast (audit item B4). A locked Keychain, or one waiting at a prompt, used to hang the run's
start, and the eval lane's pre-start gate with it. A fake `security` first on PATH stands in for the macOS tool; it
prints a fake value, never a key. $0.

Each test names the one-line mutation it catches; each was made, seen to fail the test, and reverted."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from anbud import model_client
from anbud.model_client import KeyMissing, keychain_key

FAKE = "not-a-key-0000"


def _fake_security(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    """A `security` that runs `body` (sh), first on PATH, on a platform that reads the Keychain."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool = bin_dir / "security"
    tool.write_text(f"#!/bin/sh\n{body}\n")
    tool.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(sys, "platform", "darwin")


def test_a_keychain_read_that_hangs_fails_fast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: drop `timeout=KEYCHAIN_TIMEOUT_S,` from the `security` call in `keychain_key`: the read waits for the
    tool, here 8 s, then fails for an empty answer instead of a timeout."""
    _fake_security(tmp_path, monkeypatch, f"printf '{FAKE}'; sleep 8")  # writes, then hangs as at a prompt
    monkeypatch.setattr(model_client, "KEYCHAIN_TIMEOUT_S", 0.5)
    start = time.monotonic()
    with pytest.raises(KeyMissing, match="timed out after 0.5 s") as caught:
        keychain_key()
    assert time.monotonic() - start < 4
    assert caught.value.__cause__ is None and caught.value.__suppress_context__  # what the child printed stays out
    assert FAKE not in str(caught.value)


def test_the_shipped_timeout_is_about_ten_seconds() -> None:
    assert model_client.KEYCHAIN_TIMEOUT_S == 10.0


def test_a_keychain_entry_is_read_and_a_missing_one_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_security(tmp_path, monkeypatch, f'[ "$5" = "anbud-dev" ] && printf "{FAKE}\\n" && exit 0; exit 44')
    assert keychain_key() == FAKE
    with pytest.raises(KeyMissing, match="no Keychain entry"):
        keychain_key(account="other")
