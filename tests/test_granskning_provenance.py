"""The code block a run records: git state, config, lens and prefix hashes, and nothing about the tender."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from anbud.granskning import provenance
from anbud.granskning.lenses import load_lenses


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "harness"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "a.py").write_text("x = 1\n")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "init")
    return root


def test_git_state_clean_dirty_and_unversioned(repo: Path, tmp_path: Path) -> None:
    clean = provenance.git_state(repo)
    assert clean.sha and len(clean.sha) == 40 and not clean.dirty and clean.diff_sha256 is None
    assert provenance.paid_refusal(clean) is None

    (repo / "a.py").write_text("x = 2\n")
    edited = provenance.git_state(repo)
    assert edited.sha == clean.sha and edited.dirty and edited.diff_sha256
    (repo / "new.py").write_text("y = 1\n")
    untracked = provenance.git_state(repo)
    assert untracked.diff_sha256 != edited.diff_sha256  # untracked files count toward the diff
    assert "clean tree" in (provenance.paid_refusal(untracked) or "")

    copy = tmp_path / "copy"
    copy.mkdir()
    assert provenance.git_state(copy) == provenance.GitState(None, True, None)
    inside = repo / "vendored"  # a plain copy inside another checkout is not that checkout
    inside.mkdir()
    assert provenance.git_state(inside).sha is None
    assert "git checkout" in (provenance.paid_refusal(provenance.git_state(copy)) or "")


def test_the_code_block_has_every_field_and_no_paths() -> None:
    state = provenance.GitState("d" * 40, True, "e" * 64)
    block = provenance.code_block(state)
    assert block["harness_sha"] == "d" * 40 and block["dirty"] is True and block["diff_sha256"] == "e" * 64
    assert set(block["config_sha256"]) == {"providers.toml", "prices.toml", "budgets.toml", "granskning.toml"}
    assert set(block["lens_sha256"]) == {lens.id for lens in load_lenses()}
    hashes = [*block["config_sha256"].values(), *block["lens_sha256"].values(), block["prefix_sha256"]]
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes)
    assert block["prep_version"] and "-" in block["prep_version"]
    text = json.dumps(block)
    assert "/" not in text and "\\\\" not in text and str(Path.home()) not in text
    assert provenance.code_block(state) == block  # deterministic


def test_the_prefix_hash_follows_the_brief_rule_and_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    from anbud.granskning import prefix

    before = provenance.prefix_sha256()
    monkeypatch.setattr(prefix, "RULE", prefix.RULE + " ")
    assert provenance.prefix_sha256() != before
