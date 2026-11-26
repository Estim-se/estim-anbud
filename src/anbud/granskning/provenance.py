"""The code a run ran with, for the freeze ledger: the harness commit, whether the tree was dirty, and hashes of the
config files, the lens files and the shared prefix's fixed text and tools.

It holds hashes, file names and lens ids only, never a path or a tender name. A paid run needs a clean git tree, so
its outputs can be traced to one commit; `--replay` and `--dry-run` may run dirty and record a hash of the diff.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anbud.config import CONFIG_DIR
from anbud.paths import repo_root
from anbud.store import canonical

CONFIG_FILES = ("providers.toml", "prices.toml", "budgets.toml", "granskning.toml")


@dataclass(frozen=True)
class GitState:
    sha: str | None  # None outside a git checkout
    dirty: bool
    diff_sha256: str | None  # set only for a dirty tree


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True, timeout=30).stdout


def git_state(root: Path | None = None) -> GitState:
    """HEAD and dirtiness of the repo the CLI runs from. A copy that isn't its own git checkout (even one inside
    another repo) has no sha."""
    root = (root or repo_root()).resolve()
    try:
        top = Path(_git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        if top != root:
            return GitState(None, True, None)
        sha = _git(root, "rev-parse", "HEAD").decode().strip()
        status = _git(root, "status", "--porcelain", "--untracked-files=all")
    except (OSError, subprocess.SubprocessError):
        return GitState(None, True, None)
    if not status.strip():
        return GitState(sha, False, None)
    digest = hashlib.sha256(_git(root, "diff", "HEAD", "--binary"))
    for name in sorted(_git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")):
        if name:
            digest.update(b"\0" + name + b"\0" + (root / name.decode()).read_bytes())
    return GitState(sha, True, digest.hexdigest())


def paid_refusal(state: GitState) -> str | None:
    """Why a paid run may not start on this tree, or None."""
    if state.sha is None:
        return "a paid run needs a git checkout of the harness; this copy has no commit to record"
    if state.dirty:
        return "a paid run needs a clean tree; commit or stash the changes first (--replay and --dry-run may run dirty)"
    return None


def prefix_sha256() -> str:
    """One hash over the shared prefix's fixed parts: the brief, the rule and the tools array. The corpus and calc
    blocks are the tender's and stay out."""
    from anbud.granskning.prefix import RULE, brief_text
    from anbud.tools import shared_tools

    return _sha(canonical({"brief": brief_text(), "rule": RULE, "tools": shared_tools()}))


def code_block(state: GitState, *, config_dir: Path = CONFIG_DIR) -> dict[str, Any]:
    """The `code` block for run.json and the run summary. Every lens file is hashed, whichever lenses run."""
    from anbud.granskning.lenses import LENS_DIR
    from anbud.prep.tender import code_version

    return {
        "harness_sha": state.sha,
        "dirty": state.dirty,
        "diff_sha256": state.diff_sha256,
        "config_sha256": {name: _sha((config_dir / name).read_bytes()) for name in CONFIG_FILES},
        "lens_sha256": {p.parent.name: _sha(p.read_bytes()) for p in sorted(LENS_DIR.glob("*/SKILL.md"))},
        "prefix_sha256": prefix_sha256(),
        "prep_version": code_version(),
    }
