"""`anbud guards <mode>`: run every guard over what a hook or CI hands it.

Modes:
  staged             pre-commit: staged files and their paths
  commit-msg FILE    commit-msg: the message, without git's own comment lines and the diff below the scissors line
                     (pre-push and CI scan the commit object itself, so a '#' line git keeps is still checked)
  pre-push           pre-push: for each pushed ref (stdin), the tip tree, every path and file version each new commit
                     adds or changes, the new commit objects (message and identities), tag messages, and ref names.
                     A new branch is checked over its whole history.
  ci                 CI: the same over the whole history of HEAD_SHA, plus the branch name and the PR title and body
  tree [REV]         the tracked files in the working tree, or the tree at REV
  hash-names         print the hashed names list for the ANBUD_NAMES_HMAC secret

Exit codes: 0 clean, 1 findings, 2 the guard couldn't run (it fails closed). Output never contains a listed name:
findings give a file and line, and a path that holds a name is replaced by its position.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

from anbud.guards import Finding, content, documents, licensed_text, names, sources

USAGE = __doc__
MANIFEST = "fixtures/synthetic/binary-manifest.sha256"


class UnknownMode(Exception):
    pass


_TAG_SIGNATURE = re.compile(r"\n-----BEGIN [A-Z ]*SIGNATURE-----\n.*\Z", re.S)  # appended to a tag's message
_SCISSORS = "------------------------ >8 ------------------------"


def parse_manifest(data: bytes | None) -> set[str]:
    """sha256 values of reviewed binary fixtures the guard can't fully read. One 'sha256  path' per line."""
    if not data:
        return set()
    return {line.split()[0].lower() for line in data.decode("utf-8", "replace").splitlines() if line.strip()}


class Scanner:
    """Scans paths and file versions once each and collects findings."""

    def __init__(self, matcher: names.Matcher, manifest: set[str]) -> None:
        self.matcher = matcher
        self.manifest = manifest
        self.findings: list[Finding] = []
        self.count = 0
        self._labels: dict[str, str] = {}
        self._seen: set[tuple[str, str]] = set()
        self._content_names: dict[str, list[int]] = {}

    def _name_lines(self, texts: Iterable[str]) -> list[int]:
        lines: set[int] = set()
        for text in texts:
            for variant in content.variants(text):
                lines.update(self.matcher.find_lines(variant))
        return sorted(lines)

    def label(self, path: str) -> str:
        """The path, or its position if it holds a name. CI logs of a public repo are public."""
        if path not in self._labels:
            if any(self.matcher.find_lines(t) for t in content.path_texts(sources.path_bytes(path))):
                self._labels[path] = f"path #{len(self._labels) + 1} in scan order (path hidden)"
                self.findings.append(
                    Finding("names", self._labels[path], "a listed name appears in the path (entry hidden)")
                )
            else:
                self._labels[path] = path
        return self._labels[path]

    def gitlink(self, path: str) -> None:
        self.count += 1
        self.findings.append(
            Finding("documents", self.label(path), "a submodule or nested repository; none belong here")
        )

    def file(self, path: str, data: bytes) -> None:
        digest = hashlib.sha256(data).hexdigest()
        if (path, digest) in self._seen:
            return
        self._seen.add((path, digest))
        self.count += 1
        label = self.label(path)
        reason = documents.check(path, data)
        if reason:
            self.findings.append(Finding("documents", label, reason))
            return
        in_fixtures = path.startswith(documents.ALLOWED_PREFIX)
        reviewed = digest in self.manifest
        if in_fixtures and (data.startswith(b"PK\x03\x04") or data.startswith(b"%PDF-")):
            members = content.fixture_texts(data)
            if members is None:
                if not reviewed:
                    self.findings.append(Finding("documents", label, "fixture container the guard can't read"))
                return
            if self._name_lines(text for _, text in members):
                self.findings.append(
                    Finding("names", label, "a listed name appears inside the container (entry hidden)")
                )
            if data.startswith(b"%PDF-") and not reviewed:
                self.findings.append(
                    Finding("documents", label, "fixture PDF: review it, then add its sha256 to the manifest")
                )
            return
        texts = content.decode_text(data)
        if texts is None:
            if not (in_fixtures and reviewed):
                where = (
                    "binary fixture not in the manifest" if in_fixtures else "binary file outside fixtures/synthetic/"
                )
                self.findings.append(Finding("documents", label, f"{where}; the guard can't read it"))
            return
        if digest not in self._content_names:
            self._content_names[digest] = self._name_lines(texts)
        for line in self._content_names[digest]:
            self.findings.append(Finding("names", f"{label}:{line}", "a listed name appears (entry hidden)"))
        for line, why in licensed_text.check(path, texts[0]):
            self.findings.append(Finding("licensed-text", f"{label}:{line}", why))

    def entries(self, entries: Iterable[sources.Entry]) -> None:
        entries = list(dict.fromkeys(entries))
        objects = sources.read_objects(e.sha for e in entries if not e.is_gitlink)
        for entry in entries:
            if entry.is_gitlink:
                self.gitlink(entry.path)
            else:
                self.file(entry.path, objects[entry.sha][1])

    def text(self, label: str, data: bytes | str) -> None:
        if isinstance(data, str):
            texts = [data]
        else:
            fallback = [data.decode(codec, "replace") for codec in ("utf-8", "cp1252", "mac_roman", "cp850")]
            texts = content.decode_text(data) or fallback
        for line in self._name_lines(texts):
            self.findings.append(Finding("names", f"{label} line {line}", "a listed name appears (entry hidden)"))

    def git_objects(self, shas: Iterable[str]) -> int:
        """Commit and tag objects: message, author, committer and tagger. Signatures are left out."""
        n = 0
        for sha, (kind, raw) in sources.read_objects(shas).items():
            n += 1
            text = raw.decode("latin-1")  # byte for byte, so self.text() can still try UTF-8, then cp1252
            if kind == "commit":
                head, _, message = text.partition("\n\n")
                head = "\n".join(
                    line for line in re.sub(r"\ngpgsig(?:-sha256)? .*?(?=\n\S|\Z)", "", head, flags=re.S).splitlines()
                    if not line.startswith(("tree ", "parent "))
                )  # fmt: skip
                text = head + "\n\n" + message
            elif kind == "tag":
                text = _TAG_SIGNATURE.sub("", text)
            self.text(f"{kind} {sha[:12]}", text.encode("latin-1"))
        return n


def _tags(tip: str) -> list[str]:
    """The tag object pushed, and any tag objects it points to in turn."""
    tags = []
    obj = tip
    while sources.object_type(obj) == "tag":
        tags.append(obj)
        raw = sources.read_objects([obj])[obj][1]
        obj = raw.split(b"\n", 1)[0].removeprefix(b"object ").decode()
    return tags


def _scan_range(scanner: Scanner, tip: str, selection: tuple[str, ...]) -> int:
    commits = sources.rev_list(*selection)
    entries = [*sources.tree_entries(tip), *sources.history_entries(commits)]
    # A binary fixture in history was reviewed against the manifest of its time: take every version in the range.
    versions = {e.sha for e in entries if e.path == MANIFEST and not e.is_gitlink}
    for _kind, data in sources.read_objects(versions).values():
        scanner.manifest |= parse_manifest(data)
    scanner.entries(entries)
    return scanner.git_objects([*commits, *_tags(tip)])


def mode_pre_push(stdin: Iterable[str], matcher: names.Matcher) -> tuple[list[Finding], str]:
    lines = [line.split() for line in stdin if line.strip()]
    if any(len(parts) != 4 for parts in lines):
        raise ValueError("unexpected pre-push input")
    refs = Scanner(matcher, set())
    findings: list[Finding] = []
    files = commits = 0
    for local_ref, local_sha, remote_ref, remote_sha in lines:
        if local_sha == sources.ZERO_SHA:
            continue  # a deletion pushes nothing new, and may be the cleanup of a bad branch name
        for label, ref in (("local ref name", local_ref), ("remote ref name", remote_ref)):
            refs.text(label, ref)
        if remote_sha != sources.ZERO_SHA and sources.rev_exists(remote_sha):
            selection: tuple[str, ...] = (local_sha, "--not", remote_sha)
        else:
            selection = (local_sha,)  # a new branch or an unknown remote state: its whole history
        scanner = Scanner(matcher, parse_manifest(sources.blob_at(local_sha, MANIFEST)))
        commits += _scan_range(scanner, local_sha, selection)
        findings += scanner.findings
        files += scanner.count
    return findings + refs.findings, f"{files} paths and file versions, {commits} commits and tags"


def mode_ci(env: Mapping[str, str], matcher: names.Matcher) -> tuple[list[Finding], str]:
    """The whole history every time: a run that never reached this step, or was skipped, leaves no gap."""
    head = env.get("HEAD_SHA") or "HEAD"
    scanner = Scanner(matcher, parse_manifest(sources.blob_at(head, MANIFEST)))
    commits = _scan_range(scanner, head, (head,))
    for label, var in (("branch name", "HEAD_REF"), ("PR title", "PR_TITLE"), ("PR body", "PR_BODY")):
        if env.get(var):
            scanner.text(label, env[var])
    return scanner.findings, f"{scanner.count} paths and file versions, {commits} commits"


def _hash_names(rest: list[str]) -> int:
    try:
        path = Path(rest[1]) if rest[:1] == ["--names"] and len(rest) > 1 else None
        if path is None:
            from anbud.paths import names_file

            path = names_file()
        entries = names.parse_plain(path.read_text(encoding="utf-8"))
        if not entries:
            raise names.NamesMissing("the names list is empty")
    except (OSError, names.NamesMissing) as exc:
        print(f"guards: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(names.render_hashed(entries))
    return 0


def _message_without_comments(raw: bytes) -> bytes:
    """What git keeps with its default cleanup: no comment lines, nothing from the scissors line on. The template's
    comments list file names and, with -v, the diff, which would block the commit that removes a leaked name."""
    try:
        char = sources.git("config", "core.commentChar").decode().strip()
    except sources.subprocess.CalledProcessError:
        char = "#"
    char = "#" if char in ("", "auto") else char
    kept = []
    for line in raw.split(b"\n"):
        if line.startswith(char.encode()):
            if _SCISSORS.encode() in line:
                break
            continue
        kept.append(line)
    return b"\n".join(kept)


def _run(mode: str, rest: list[str], env: Mapping[str, str], matcher: names.Matcher) -> tuple[list[Finding], str]:
    if mode == "staged":
        scanner = Scanner(matcher, parse_manifest(sources.blob_at("", MANIFEST)))
        scanner.entries(sources.staged_entries())
        return scanner.findings, f"{scanner.count} staged files"
    if mode == "commit-msg":
        scanner = Scanner(matcher, set())
        scanner.text("commit message", _message_without_comments(Path(rest[0]).read_bytes()))
        return scanner.findings, "commit message"
    if mode == "pre-push":
        return mode_pre_push(sys.stdin, matcher)
    if mode == "ci":
        return mode_ci(env, matcher)
    if mode == "tree" and rest:
        scanner = Scanner(matcher, parse_manifest(sources.blob_at(rest[0], MANIFEST)))
        scanner.entries(sources.tree_entries(rest[0]))
        return scanner.findings, f"{scanner.count} files"
    if mode == "tree":
        root = Path(sources.git("rev-parse", "--show-toplevel").decode().strip())
        manifest_file = root / MANIFEST
        scanner = Scanner(matcher, parse_manifest(manifest_file.read_bytes() if manifest_file.exists() else None))
        for record in sources.git("ls-files", "-s", "-z", cwd=root).split(b"\0"):
            if not record:
                continue
            meta, raw = record.split(b"\t", 1)
            path, where = raw.decode("utf-8", "surrogateescape"), root / os.fsdecode(raw)
            if meta.startswith(sources.GITLINK.encode()):
                scanner.gitlink(path)
            elif not where.is_symlink() and not where.exists():
                continue  # deleted in the working tree: nothing to scan there
            elif where.is_symlink():
                scanner.file(path, os.fsencode(os.readlink(where)))
            else:
                scanner.file(path, where.read_bytes())
        return scanner.findings, f"{scanner.count} files"
    raise UnknownMode(mode)


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    for stream in (sys.stdout, sys.stderr):  # a non-UTF-8 path must not crash printing
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 2
    mode, rest = argv[0], argv[1:]
    if mode == "hash-names":
        return _hash_names(rest)

    try:
        matcher = names.load_matcher(env)
    except names.NamesMissing as exc:
        print(f"guards: cannot run the names guard: {exc}", file=sys.stderr)
        return 2

    try:
        findings, summary = _run(mode, rest, env, matcher)
    except UnknownMode:
        print(USAGE, file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - fail closed, and print no message: it could hold a path or a name
        print(f"guards: cannot run: {type(exc).__name__}", file=sys.stderr)
        return 2

    unique = sorted(set(findings), key=str)
    for finding in unique:
        print(finding)
    if unique:
        print(f"guards: {len(unique)} finding(s) in {summary}", file=sys.stderr)
        return 1
    print(f"guards: OK ({summary})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
