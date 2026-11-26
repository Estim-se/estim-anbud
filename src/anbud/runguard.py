"""Who may run: `anbud run` takes dev tenders and synthetic fixtures only.

Held-out, prospective and unknown tenders are refused, and so is anything that resolves into held-out or sealed
storage. Only `anbud eval heldout` / `anbud eval prospective` may touch held-out data, and only when a person runs it
at a terminal, never from an agent session.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO

from anbud import paths
from anbud.evals import split_heldout


class RunRefused(Exception):
    """Raised with a reason that names no tender content."""


def check_run_allowed(
    company_id: str, tender_id: str | None, fixture: Path | None = None, root: Path | None = None
) -> str:
    try:
        return _check_run_allowed(company_id, tender_id, fixture, root)
    except (RuntimeError, OSError):  # a symlink loop, say: refuse rather than crash
        raise RunRefused("a path couldn't be resolved; fix the data folder before running") from None


def _check_run_allowed(company_id: str, tender_id: str | None, fixture: Path | None, root: Path | None) -> str:
    if fixture is not None:
        if tender_id:
            raise RunRefused("give a tender id or a fixture, not both")
        allowed = (paths.repo_root() / "fixtures" / "synthetic").resolve()
        resolved = fixture.expanduser().resolve()
        if not (resolved == allowed or allowed in resolved.parents):
            raise RunRefused("fixtures must live in fixtures/synthetic/")
        if paths.is_sealed_path(resolved, root):
            raise RunRefused("the fixture path resolves into held-out or sealed storage")
        return "fixture"
    if not tender_id:
        raise RunRefused("give a tender id or --fixture")
    try:
        tender_dir = paths.dev_tender_dir(company_id, tender_id, root)
    except ValueError as exc:
        raise RunRefused(str(exc)) from None
    if paths.is_sealed_path(tender_dir, root):
        raise RunRefused("the tender path resolves into held-out or sealed storage")
    try:
        status = split_heldout.status_of(company_id, tender_id, root)
    except (split_heldout.SplitError, ValueError, KeyError):
        raise RunRefused("the split ledger is unreadable; fix it before running") from None
    if status is None:
        raise RunRefused("the tender isn't in the split ledger; run `anbud split` first")
    if status != split_heldout.DEV:
        raise RunRefused("only dev tenders and synthetic fixtures may run")
    return "dev"


AGENT_VARIABLES = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "AI_AGENT")
AGENT_PREFIXES = ("CODEX_",)


def in_agent_session(env: Mapping[str, str] | None = None) -> bool:
    """A backup signal only: agents can run without these variables. held_out_refusal() also demands a terminal."""
    env = os.environ if env is None else env
    return any(env.get(name) for name in AGENT_VARIABLES) or any(k.startswith(AGENT_PREFIXES) for k in env)


def held_out_refusal(
    env: Mapping[str, str] | None = None, stdin: TextIO | None = None, stdout: TextIO | None = None
) -> str | None:
    """Why `anbud eval heldout|prospective` must not run here, or None. It fails closed: a person at a terminal only."""
    if in_agent_session(env):
        return "never from an agent session"
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    if not (stdin.isatty() and stdout.isatty()):
        return "only from a terminal, run by a person"
    return None
