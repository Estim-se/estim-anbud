"""What the guards read from git: index entries, a commit's tree, every entry each commit in a range changes, commit
and tag objects. Paths come from `-z` output, so any byte in a path survives; blob contents come from one
`git cat-file --batch` process.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

ZERO_SHA = "0" * 40
GITLINK = "160000"


@dataclass(frozen=True)
class Entry:
    path: str
    mode: str  # "100644", "100755", "120000" (symlink: the blob is the target), "160000" (gitlink)
    sha: str

    @property
    def is_gitlink(self) -> bool:
        return self.mode == GITLINK


def git(*args: str, cwd: Path | None = None, stdin: bytes | None = None) -> bytes:
    # Replace refs would make git show one history while a push sends another.
    command = ["git", "--no-replace-objects", *args]
    return subprocess.run(command, cwd=cwd, input=stdin, check=True, capture_output=True).stdout


def toplevel() -> Path:
    return Path(git("rev-parse", "--show-toplevel").decode().strip())


def _decode_path(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def path_bytes(path: str) -> bytes:
    return path.encode("utf-8", "surrogateescape")


def staged_entries() -> list[Entry]:
    """Index entries for every staged change except deletions (added, copied, modified, renamed, type-changed)."""
    top = toplevel()  # both listings relative to the top, whatever directory the hook runs in
    listing = git("diff", "--cached", "--name-only", "--diff-filter=d", "--ignore-submodules=none", "-z", cwd=top)
    changed = {p for p in listing.split(b"\0") if p}
    entries = []
    for record in git("ls-files", "-s", "-z", cwd=top).split(b"\0"):
        if not record:
            continue
        meta, raw_path = record.split(b"\t", 1)
        if raw_path not in changed:
            continue
        mode, sha, _stage = meta.decode().split(" ")
        entries.append(Entry(_decode_path(raw_path), mode, sha))
    return entries


def tree_entries(rev: str) -> list[Entry]:
    entries = []
    for record in git("ls-tree", "-r", "-z", "--full-tree", rev).split(b"\0"):
        if not record:
            continue
        meta, raw_path = record.split(b"\t", 1)
        mode, _kind, sha = meta.decode().split(" ")
        entries.append(Entry(_decode_path(raw_path), mode, sha))
    return entries


def rev_list(*rev_args: str) -> list[str]:
    return git("rev-list", *rev_args, "--").decode().split()


def history_entries(commits: Iterable[str]) -> list[Entry]:
    """Every entry each commit adds or changes, against each parent for merges, renames split into add + delete.

    Unlike `rev-list --objects` (one path per object), this sees every path a blob was ever committed under, so a name
    in a file or folder name that a later commit renames is still found.
    """
    commits = list(commits)
    if not commits:
        return []
    out = git(
        "diff-tree", "--stdin", "-r", "-z", "--root", "-m", "--no-commit-id", "--no-renames", "--no-ext-diff",
        "--ignore-submodules=none", "--diff-filter=d",
        stdin=("\n".join(commits) + "\n").encode(),
    )  # fmt: skip
    entries = []
    fields = out.split(b"\0")
    i = 0
    while i < len(fields):
        meta = fields[i]
        if not meta.startswith(b":"):
            i += 1
            continue
        _src_mode, dst_mode, _src_sha, dst_sha, _status = meta[1:].decode().split(" ")
        entries.append(Entry(_decode_path(fields[i + 1]), dst_mode, dst_sha))
        i += 2
    return entries


def read_objects(shas: Iterable[str]) -> dict[str, tuple[str, bytes]]:
    """sha -> (type, content) for each object, from one `git cat-file --batch` process."""
    wanted = list(dict.fromkeys(shas))
    if not wanted:
        return {}
    out = git("cat-file", "--batch", stdin=("\n".join(wanted) + "\n").encode())
    objects: dict[str, tuple[str, bytes]] = {}
    pos = 0
    for sha in wanted:
        end = out.index(b"\n", pos)
        header = out[pos:end].decode().split(" ")
        if len(header) != 3:  # "<sha> missing"
            raise ValueError("git object missing")
        size = int(header[2])
        objects[sha] = (header[1], out[end + 1 : end + 1 + size])
        pos = end + 1 + size + 1
    return objects


def rev_exists(rev: str) -> bool:
    try:
        git("cat-file", "-e", f"{rev}^{{commit}}")
        return True
    except subprocess.CalledProcessError:
        return False


def object_type(rev: str) -> str:
    return git("cat-file", "-t", rev).decode().strip()


def blob_at(rev: str, path: str) -> bytes | None:
    """A file's content at a revision (`:` for the index), or None if it isn't there."""
    try:
        return git("cat-file", "blob", f"{rev}:{path}")
    except subprocess.CalledProcessError:
        return None
