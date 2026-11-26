"""Folder mode: the tender's files as delivered, read in place and never written.

Files are identified by content, deduplicated by hash, and checked against the intake manifest when there is one.
Nothing here prints a file name: names often carry the customer's or the project's name.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from anbud.prep.model import DocKind
from anbud.prep.text import doc_id_of

MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_UNZIPPED_BYTES = 1024 * 1024 * 1024
MAX_ZIP_ENTRIES = 20_000


class SourceError(RuntimeError):
    """Raised with a reason that names no file."""


@dataclass(frozen=True)
class Source:
    sha256: str
    kind: DocKind
    size: int
    paths: tuple[str, ...]  # relative to the folder; local only, never logged
    data: bytes = field(repr=False, compare=False)

    @property
    def doc_id(self) -> str:
        return doc_id_of(self.sha256)


def _zip_kind(data: bytes) -> DocKind:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_UNZIPPED_BYTES:
                raise SourceError("a zip container unpacks to more than the limit")
            names = {i.filename for i in infos}
    except zipfile.BadZipFile:
        return "other"
    if "xl/workbook.xml" in names:
        return "xlsx"
    if "word/document.xml" in names:
        return "docx"
    return "archive"


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
    if Path(name).suffix.lower() in (".txt", ".md") and b"\x00" not in head:
        return "text"
    return "other"


def read_manifest(path: Path) -> set[str]:
    """Hashes from a `shasum -a 256` style manifest: `<sha256>  <relative path>` per line."""
    hashes = set()
    for line in path.read_text(encoding="utf-8").splitlines():
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


def load_folder(folder: Path, manifest: Path | None = None) -> Loaded:
    if not folder.is_dir():
        raise SourceError("the tender has no inbox folder")
    found: dict[str, tuple[bytes, list[str]]] = {}
    links = hidden = 0
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder)
        if any(part.startswith(".") for part in rel.parts):
            hidden += path.is_file()
            continue
        if path.is_symlink():
            links += 1
            continue
        if not path.is_file():
            continue
        if path.stat().st_size > MAX_FILE_BYTES:
            raise SourceError("a file is larger than the per-file limit")
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        entry = found.setdefault(digest, (data, []))
        entry[1].append(rel.as_posix())
    if manifest is not None and manifest.exists():
        expected = read_manifest(manifest)
        extra, missing = len(set(found) - expected), len(expected - set(found))
        if extra or missing:
            raise SourceError(
                f"the inbox doesn't match its intake manifest ({extra} unexpected, {missing} missing or changed)"
            )
    sources = [
        Source(digest, detect_kind(data, rels[0]), len(data), tuple(rels), data)
        for digest, (data, rels) in sorted(found.items(), key=lambda kv: kv[1][1][0])
    ]
    return Loaded(sources, links, hidden)
