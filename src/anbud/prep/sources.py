"""Folder mode: the tender's files as delivered, read in place and never written.

Files are identified by content, deduplicated by hash, and checked against the intake manifest when there is one.
Nothing here prints a file name: names often carry the customer's or the project's name.

- The inbox and the manifest must be real folders and files: a symlink, or a file that resolves outside the inbox, is
  refused rather than followed.
- A file over the per-file limit, or a zip container that unpacks past the limit, is hashed in a stream and listed as
  skipped ("too large"); its bytes are never held in memory.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from anbud.prep.html import looks_like_mime
from anbud.prep.model import DocKind
from anbud.prep.text import doc_id_of

MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_UNZIPPED_BYTES = 1024 * 1024 * 1024
MAX_ZIP_ENTRIES = 20_000
_HTML_SUFFIXES = (".html", ".htm", ".xhtml")
_MHTML_SUFFIXES = (".mht", ".mhtml")


class SourceError(RuntimeError):
    """Raised with a reason that names no file."""


@dataclass(frozen=True)
class Source:
    sha256: str
    kind: DocKind
    size: int
    paths: tuple[str, ...]  # relative to the folder; local only, never logged
    data: bytes = field(repr=False, compare=False)
    too_large: bool = False

    @property
    def doc_id(self) -> str:
        return doc_id_of(self.sha256)


class _TooLarge(Exception):
    pass


def _zip_kind(data: bytes) -> DocKind:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_UNZIPPED_BYTES:
                raise _TooLarge
            names = {i.filename for i in infos}
    except (zipfile.BadZipFile, ValueError, OSError):
        return "other"
    if "xl/workbook.xml" in names:
        return "xlsx"
    if "word/document.xml" in names:
        return "docx"
    return "archive"


def _looks_like_html(head: bytes) -> bool:
    """Markup at the start, after a byte order mark, whitespace and comments such as a browser's "saved from" line."""
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        head = head.decode("utf-16", errors="ignore").encode("ascii", errors="ignore")
    text = head.removeprefix(b"\xef\xbb\xbf").lstrip().lower()
    while text.startswith(b"<!--"):
        end = text.find(b"-->")
        if end < 0:
            return False
        text = text[end + 3 :].lstrip()
    if text.startswith((b"<!doctype html", b"<html")):
        return True
    return text.startswith(b"<?xml") and b"<html" in text


def _html_kind(data: bytes, suffix: str) -> bool:
    """A saved web page: HTML by content or by name, MHTML, or a Safari web archive."""
    head = data[:1024]
    if suffix == ".webarchive" and head.startswith(b"bplist00"):
        return True
    if suffix in _MHTML_SUFFIXES or (looks_like_mime(data[:4096]) and b"multipart/related" in data[:4096].lower()):
        return True
    return _looks_like_html(head) or (suffix in _HTML_SUFFIXES and b"\x00" not in head)


def detect_kind(data: bytes, name: str) -> DocKind:
    head = data[:1024]
    if b"%PDF-" in head:
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return _zip_kind(data)
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "legacy_office"
    if head.startswith(b"AC10"):
        return "dwg"
    if head.lstrip().startswith(b"ISO-10303-21"):
        return "ifc"
    if head.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"II*\x00", b"MM\x00*")):
        return "image"
    if head.startswith((b"Rar!", b"7z\xbc\xaf\x27\x1c", b"\x1f\x8b")):
        return "archive"
    suffix = Path(name).suffix.lower()
    if suffix in (".txt", ".md") and b"\x00" not in head:
        return "text"  # as before web pages were read: a note stays a note, whatever markup it starts with
    if _html_kind(data, suffix):
        return "html"
    return "other"


def read_manifest(path: Path) -> set[str]:
    """Hashes from a `shasum -a 256` style manifest: `<sha256>  <relative path>` per line."""
    if path.is_symlink() or not path.is_file():
        raise SourceError("the intake manifest is not a regular file")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise SourceError("the intake manifest can't be read") from None
    hashes = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        digest = line.split(maxsplit=1)[0].lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise SourceError("the intake manifest has a line that isn't a sha256")
        hashes.add(digest)
    return hashes


@dataclass
class Loaded:
    sources: list[Source]
    skipped_links: int = 0
    skipped_hidden: int = 0


def _stream_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load_folder(folder: Path, manifest: Path | None = None) -> Loaded:
    if folder.is_symlink() or not folder.is_dir():
        raise SourceError("the tender has no inbox folder, or it is a link")
    base = folder.resolve()
    found: dict[str, tuple[bytes, list[str], bool, int]] = {}
    links = hidden = 0
    try:
        for path in sorted(folder.rglob("*")):
            rel = path.relative_to(folder)
            if any(part.startswith(".") for part in rel.parts):
                hidden += path.is_file()
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(base):
                links += 1
                continue
            if not path.is_file():
                continue
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                data, digest, too_large = b"", _stream_hash(path), True
            else:
                data = path.read_bytes()
                digest, too_large = hashlib.sha256(data).hexdigest(), False
            entry = found.setdefault(digest, (data, [], too_large, size))
            entry[1].append(rel.as_posix())
    except OSError:
        raise SourceError("a file in the inbox can't be read") from None
    if manifest is not None and (manifest.exists() or manifest.is_symlink()):
        expected = read_manifest(manifest)
        extra, missing = len(set(found) - expected), len(expected - set(found))
        if extra or missing:
            raise SourceError(
                f"the inbox doesn't match its intake manifest ({extra} unexpected, {missing} missing or changed)"
            )
    sources = []
    for digest, (data, rels, too_large, size) in sorted(found.items(), key=lambda kv: kv[1][1][0]):
        kind: DocKind = "other"
        if not too_large:
            try:
                kind = detect_kind(data, rels[0])
            except _TooLarge:
                too_large, data = True, b""
        sources.append(Source(digest, kind, size, tuple(rels), data, too_large))
    return Loaded(sources, links, hidden)
