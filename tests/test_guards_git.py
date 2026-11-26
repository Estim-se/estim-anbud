"""The guards end to end in a throwaway git repo, as the hooks and CI run them."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import assert_no_fake_names


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Test")
    git(path, "config", "commit.gpgsign", "false")
    (path / "ok.txt").write_text("nothing to see\n", encoding="utf-8")
    git(path, "add", "ok.txt")
    git(path, "commit", "-q", "-m", "start")
    return path


def run_guard(repo: Path, env: dict[str, str], *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if k not in ("CI", "ANBUD_NAMES_HMAC", "ANBUD_NAMES_FILE")}
    clean.update(env)
    return subprocess.run(
        [sys.executable, "-m", "anbud.guards", *args],
        cwd=repo,
        env=clean,
        input=stdin,
        capture_output=True,
        text=True,
    )


def test_staged_file_with_a_name_is_refused_without_printing_it(repo: Path, local_env: dict[str, str]) -> None:
    (repo / "notes.md").write_text("Möte med Påhittade Fastigheter i morgon\n", encoding="utf-8")
    git(repo, "add", "notes.md")
    result = run_guard(repo, local_env, "staged")
    assert result.returncode == 1
    assert "notes.md:1" in result.stdout
    assert_no_fake_names(result.stdout, result.stderr)


def test_name_in_a_path_is_refused_and_the_path_is_not_printed(repo: Path, local_env: dict[str, str]) -> None:
    (repo / "fiktivbolaget-ab-offert.txt").write_text("text\n", encoding="utf-8")
    git(repo, "add", ".")
    result = run_guard(repo, local_env, "staged")
    assert result.returncode == 1
    assert "path hidden" in result.stdout
    assert_no_fake_names(result.stdout, result.stderr)


def test_staged_document_is_refused(repo: Path, local_env: dict[str, str]) -> None:
    (repo / "bilaga.bin").write_bytes(b"%PDF-1.7 synthetic")
    git(repo, "add", ".")
    result = run_guard(repo, local_env, "staged")
    assert result.returncode == 1
    assert "documents: bilaga.bin: PDF content" in result.stdout


def test_commit_message_with_a_name_is_refused(repo: Path, local_env: dict[str, str], tmp_path: Path) -> None:
    message = tmp_path / "MSG"
    message.write_text("Fix för Örjan Exempelsson\n# a comment line\n", encoding="utf-8")
    result = run_guard(repo, local_env, "commit-msg", str(message))
    assert result.returncode == 1
    assert_no_fake_names(result.stdout, result.stderr)


def test_pre_push_checks_tree_messages_and_branch(repo: Path, local_env: dict[str, str]) -> None:
    git(repo, "checkout", "-q", "-b", "ampere-fix")
    (repo / "a.txt").write_text("clean\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "clean message")
    sha = git(repo, "rev-parse", "HEAD").strip()
    zero = "0" * 40
    clean = run_guard(repo, local_env, "pre-push", stdin=f"refs/heads/ampere-fix {sha} refs/heads/ampere-fix {zero}\n")
    assert clean.returncode == 0, clean.stdout + clean.stderr  # 'ampere' lower-case is not the listed 'Ampere'
    named = run_guard(repo, local_env, "pre-push", stdin=f"refs/heads/Ampere {sha} refs/heads/Ampere {zero}\n")
    assert named.returncode == 1
    assert_no_fake_names(named.stdout, named.stderr)


def test_ci_mode_uses_only_the_hashed_secret(repo: Path, names_file: Path) -> None:
    hashed = run_guard(repo, {}, "hash-names", "--names", str(names_file)).stdout
    sha = git(repo, "rev-parse", "HEAD").strip()
    env = {"CI": "true", "ANBUD_NAMES_HMAC": hashed, "HEAD_SHA": sha, "PR_TITLE": "Klart för fiktivbolaget ab"}
    result = run_guard(repo, env, "ci")
    assert result.returncode == 1
    assert "PR title line 1" in result.stdout
    assert_no_fake_names(result.stdout, result.stderr)
    missing = run_guard(repo, {"CI": "true", "ANBUD_NAMES_FILE": str(names_file)}, "ci")
    assert missing.returncode == 2  # fails closed: CI never reads a plain list


def test_clean_repo_passes(repo: Path, local_env: dict[str, str]) -> None:
    result = run_guard(repo, local_env, "tree")
    assert result.returncode == 0, result.stdout + result.stderr


def _add_then_remove(repo: Path) -> tuple[str, str]:
    """A name added in one commit and deleted in the next: the tip tree is clean, the history isn't."""
    base = git(repo, "rev-parse", "HEAD").strip()
    (repo / "notes.md").write_text("Möte med Påhittade Fastigheter\n", encoding="utf-16")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "--no-verify", "-m", "add notes")
    git(repo, "rm", "-q", "notes.md")
    git(repo, "commit", "-q", "--no-verify", "-m", "remove notes")
    return base, git(repo, "rev-parse", "HEAD").strip()


def test_pre_push_scans_every_pushed_version_not_just_the_tip(repo: Path, local_env: dict[str, str]) -> None:
    base, head = _add_then_remove(repo)
    zero = "0" * 40
    new_branch = run_guard(repo, local_env, "pre-push", stdin=f"refs/heads/main {head} refs/heads/main {zero}\n")
    assert new_branch.returncode == 1, new_branch.stdout + new_branch.stderr
    update = run_guard(repo, local_env, "pre-push", stdin=f"refs/heads/main {head} refs/heads/main {base}\n")
    assert update.returncode == 1
    assert_no_fake_names(new_branch.stdout, new_branch.stderr, update.stdout, update.stderr)


def test_ci_scans_every_version_in_the_range(repo: Path, names_file: Path) -> None:
    base, head = _add_then_remove(repo)
    hashed = run_guard(repo, {}, "hash-names", "--names", str(names_file)).stdout
    env = {"CI": "true", "ANBUD_NAMES_HMAC": hashed, "HEAD_SHA": head, "BASE_SHA": base}
    result = run_guard(repo, env, "ci")
    assert result.returncode == 1, result.stdout + result.stderr
    no_base = run_guard(repo, {**env, "BASE_SHA": "0" * 40}, "ci")  # a new branch: the whole history
    assert no_base.returncode == 1
    assert_no_fake_names(result.stdout, result.stderr, no_base.stdout, no_base.stderr)


def test_commit_msg_skips_gits_template_but_pre_push_checks_what_git_kept(
    repo: Path, local_env: dict[str, str], tmp_path: Path
) -> None:
    message = tmp_path / "MSG"
    template = "Ta bort filen\n# Changes to be committed:\n#\tdeleted:    Örjan Exempelsson.txt\n"
    message.write_text(template, encoding="utf-8")
    assert run_guard(repo, local_env, "commit-msg", str(message)).returncode == 0  # the cleanup commit goes through
    message.write_text("Fix för Örjan Exempelsson\n", encoding="utf-8")
    assert run_guard(repo, local_env, "commit-msg", str(message)).returncode == 1
    message.write_bytes("Fix för Påhittade Fastigheter\n".encode("cp1252"))
    assert run_guard(repo, local_env, "commit-msg", str(message)).returncode == 1
    # `git commit -m` keeps '#' lines; the commit object is checked on push.
    head = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "commit", "-q", "--no-verify", "--allow-empty", "-m", "#12 rättning för Örjan Exempelsson")
    new = git(repo, "rev-parse", "HEAD").strip()
    pushed = run_guard(repo, local_env, "pre-push", stdin=f"refs/heads/main {new} refs/heads/main {head}\n")
    assert pushed.returncode == 1
    assert_no_fake_names(pushed.stdout, pushed.stderr)


def test_a_type_change_is_scanned(repo: Path, local_env: dict[str, str]) -> None:
    (repo / "link").symlink_to("ok.txt")
    git(repo, "add", "link")
    git(repo, "commit", "-q", "--no-verify", "-m", "link")
    (repo / "link").unlink()
    (repo / "link").write_text("Fiktivbolaget AB\n", encoding="utf-8")
    git(repo, "add", "link")
    result = run_guard(repo, local_env, "staged")
    assert result.returncode == 1
    assert_no_fake_names(result.stdout, result.stderr)


def _push_check(repo: Path, env: dict[str, str], ref: str, sha: str, remote_sha: str = "0" * 40):
    result = run_guard(repo, env, "pre-push", stdin=f"{ref} {sha} {ref} {remote_sha}\n")
    assert_no_fake_names(result.stdout, result.stderr)
    return result


def test_a_name_in_a_folder_renamed_later_is_found_in_pre_push_and_ci(repo: Path, names_file: Path) -> None:
    base = git(repo, "rev-parse", "HEAD").strip()
    folder = repo / "kunder" / "Fiktivbolaget AB"
    folder.mkdir(parents=True)
    (folder / "kalkyl.md").write_text("ren text\n", encoding="utf-8")
    (repo / "Örjan Exempelsson.txt").write_text("", encoding="utf-8")  # an empty blob: reused, never new
    git(repo, "add", ".")
    git(repo, "commit", "-q", "--no-verify", "-m", "add")
    git(repo, "mv", "kunder/Fiktivbolaget AB", "kunder/t-0001")
    git(repo, "rm", "-q", "Örjan Exempelsson.txt")
    git(repo, "commit", "-q", "--no-verify", "-m", "rename")
    head = git(repo, "rev-parse", "HEAD").strip()
    env = {"ANBUD_NAMES_FILE": str(names_file)}
    assert _push_check(repo, env, "refs/heads/main", head, base).returncode == 1
    hashed = run_guard(repo, {}, "hash-names", "--names", str(names_file)).stdout
    ci = run_guard(repo, {"CI": "true", "ANBUD_NAMES_HMAC": hashed, "HEAD_SHA": head, "BASE_SHA": base}, "ci")
    assert ci.returncode == 1, ci.stdout + ci.stderr
    assert_no_fake_names(ci.stdout, ci.stderr)


def test_a_new_branch_is_checked_over_its_whole_history(repo: Path, local_env: dict[str, str]) -> None:
    _base, head = _add_then_remove(repo)
    git(repo, "update-ref", "refs/remotes/private/main", head)  # already on another remote
    assert _push_check(repo, local_env, "refs/heads/main", head).returncode == 1


def test_tag_messages_and_commit_identities_are_scanned(repo: Path, local_env: dict[str, str]) -> None:
    git(repo, "tag", "-a", "v1", "-m", "Leverans till Fiktivbolaget AB")
    tag = git(repo, "rev-parse", "v1").strip()
    head = git(repo, "rev-parse", "HEAD").strip()
    assert _push_check(repo, local_env, "refs/tags/v1", tag, "0" * 40).returncode == 1
    git(repo, "-c", "user.name=Örjan Exempelsson", "commit", "-q", "--no-verify", "--allow-empty", "-m", "ren")
    new = git(repo, "rev-parse", "HEAD").strip()
    assert _push_check(repo, local_env, "refs/heads/main", new, head).returncode == 1


def test_a_nested_repository_is_refused(repo: Path, local_env: dict[str, str]) -> None:
    nested = repo / "inner"
    nested.mkdir()
    git(nested, "init", "-q")
    git(nested, "-c", "user.email=a@example.invalid", "-c", "user.name=A", "commit", "-q", "--allow-empty", "-m", "x")
    git(repo, "add", "inner")
    result = run_guard(repo, local_env, "staged")
    assert result.returncode == 1 and "nested repository" in result.stdout


def test_a_non_utf8_path_in_the_index_is_decoded(repo: Path, local_env: dict[str, str]) -> None:
    blob = (
        subprocess.run(
            ["git", "hash-object", "-w", "--stdin"], cwd=repo, input=b"ren\n", capture_output=True, check=True
        )
        .stdout.decode()
        .strip()
    )
    raw_path = "Påhittade Fastigheter.txt".encode("cp1252")
    subprocess.run(
        [b"git", b"update-index", b"--add", b"--cacheinfo", b"100644," + blob.encode() + b"," + raw_path],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    result = run_guard(repo, local_env, "staged")
    assert result.returncode == 1 and "path hidden" in result.stdout
    assert_no_fake_names(result.stdout, result.stderr)


def test_the_manifest_comes_from_the_index_not_the_working_tree(repo: Path, local_env: dict[str, str]) -> None:
    import hashlib

    fixture = repo / "fixtures" / "synthetic"
    fixture.mkdir(parents=True)
    data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    (fixture / "a.png").write_bytes(data)
    git(repo, "add", "-f", "fixtures/synthetic/a.png")
    (fixture / "binary-manifest.sha256").write_text(f"{hashlib.sha256(data).hexdigest()}  a.png\n", encoding="utf-8")
    assert run_guard(repo, local_env, "staged").returncode == 1  # the manifest isn't staged
    git(repo, "add", "fixtures/synthetic/binary-manifest.sha256")
    assert run_guard(repo, local_env, "staged").returncode == 0


def test_errors_never_print_a_traceback(repo: Path, local_env: dict[str, str]) -> None:
    result = run_guard(repo, local_env, "pre-push", stdin="not four fields\n")
    assert result.returncode == 2 and "Traceback" not in result.stderr


def test_a_replaced_fixture_keeps_passing_over_whole_history(repo: Path, local_env: dict[str, str]) -> None:
    import hashlib

    fixture = repo / "fixtures" / "synthetic"
    fixture.mkdir(parents=True)
    manifest = fixture / "binary-manifest.sha256"
    for version in (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, b"\x89PNG\r\n\x1a\n" + b"\x01" * 32):
        (fixture / "a.png").write_bytes(version)
        manifest.write_text(f"{hashlib.sha256(version).hexdigest()}  a.png\n", encoding="utf-8")  # only the latest
        git(repo, "add", "-f", "fixtures")
        git(repo, "commit", "-q", "--no-verify", "-m", "fixture")
    head = git(repo, "rev-parse", "HEAD").strip()
    result = _push_check(repo, local_env, "refs/heads/feature", head)  # a new branch: whole history
    assert result.returncode == 0, result.stdout + result.stderr


def test_deleting_a_badly_named_branch_is_allowed(repo: Path, local_env: dict[str, str]) -> None:
    zero = "0" * 40
    result = run_guard(repo, local_env, "pre-push", stdin=f"(delete) {zero} refs/heads/Fiktivbolaget-AB {'1' * 40}\n")
    assert result.returncode == 0, result.stdout + result.stderr


def test_nested_tags_and_quoted_signatures_are_scanned(repo: Path, local_env: dict[str, str]) -> None:
    git(repo, "tag", "-a", "inner", "-m", "Leverans till Fiktivbolaget AB")
    git(repo, "tag", "-a", "outer", "-m", "ren", "inner")
    outer = git(repo, "rev-parse", "outer").strip()
    assert _push_check(repo, local_env, "refs/tags/outer", outer).returncode == 1
    head = git(repo, "rev-parse", "HEAD").strip()
    body = "ren\n\n-----BEGIN PGP SIGNATURE-----\nÖrjan Exempelsson\n-----END PGP SIGNATURE-----\n"
    git(repo, "commit", "-q", "--no-verify", "--allow-empty", "-m", body)
    new = git(repo, "rev-parse", "HEAD").strip()
    assert _push_check(repo, local_env, "refs/heads/main", new, head).returncode == 1


def test_staged_works_from_a_subdirectory_and_sees_ignored_submodules(repo: Path, local_env: dict[str, str]) -> None:
    sub = repo / "docs"
    sub.mkdir()
    (sub / "a.md").write_text("Möte med Påhittade Fastigheter\n", encoding="utf-8")
    git(repo, "add", "docs/a.md")
    result = subprocess.run(
        [sys.executable, "-m", "anbud.guards", "staged"],
        cwd=sub,
        env={k: v for k, v in os.environ.items() if k not in ("CI", "ANBUD_NAMES_HMAC")} | local_env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    git(repo, "reset", "-q")
    git(repo, "config", "diff.ignoreSubmodules", "all")
    nested = repo / "inner"
    nested.mkdir()
    git(nested, "init", "-q")
    git(nested, "-c", "user.email=a@example.invalid", "-c", "user.name=A", "commit", "-q", "--allow-empty", "-m", "x")
    git(repo, "add", "inner")
    assert run_guard(repo, local_env, "staged").returncode == 1


def test_a_non_utf8_names_list_exits_2_without_a_traceback(repo: Path, tmp_path: Path) -> None:
    latin = tmp_path / "latin.txt"
    latin.write_bytes("Påhittade Fastigheter\n".encode("cp1252"))
    result = run_guard(repo, {"ANBUD_NAMES_FILE": str(latin)}, "tree")
    assert result.returncode == 2 and "Traceback" not in result.stderr
