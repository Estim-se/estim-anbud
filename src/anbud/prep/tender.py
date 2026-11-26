"""`anbud prep`: a tender's files to blocks, an outline, tiles and a masking report.

Each file's result is cached in the company's folder under its content hash and a hash of the prep code and library
versions. Re-running is free, and any code change prepares every file again. Tiles are PNGs in the company's blob
store. For the tender, prep writes into its `prep/` folder:

    documents.json   one entry per file: id, kind, size, pages, sheets, counts, and its local path(s)
    blocks.jsonl     every block, masked, in document order
    outline.json     headings, bookmarks and sheets
    tiles.json       tile metadata; the PNGs are blobs
    masking.jsonl    every masked span with its original text (local, like the files themselves)
    summary.json     counts only

A security marking anywhere stops the tender before any of these are written.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import io
import json
import zipfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import ParseError

import pypdfium2 as pdfium
from defusedxml import DefusedXmlException
from openpyxl.utils.exceptions import InvalidFileException
from PIL import Image, UnidentifiedImageError

from anbud import models
from anbud.prep.build import DocBuilder, Piece
from anbud.prep.docx import read_docx
from anbud.prep.model import DocPrep, Marking, PageInfo, Tile
from anbud.prep.pdf import read_pdf, tile_grid
from anbud.prep.sheets import read_xlsx
from anbud.prep.sources import Loaded, Source, load_folder
from anbud.prep.text import DOC_PREFIX
from anbud.store import RunStore, StoreError, canonical

PREP_VERSION = "1"
_LIBRARIES = ("pypdfium2", "openpyxl", "pillow", "defusedxml")
MAX_IMAGE_PIXELS = 120_000_000


class PrepError(RuntimeError):
    """Raised with a reason that names no file and quotes no text."""


class IntakeStop(PrepError):
    def __init__(self, found: list[Marking]) -> None:
        kinds = ", ".join(sorted({m.kind for m in found}))
        first = found[0]
        where = f"block {first.block_id}" if first.block_id else "an outline title or sheet name"
        super().__init__(
            f"{len(found)} security marking(s) ({kinds}), the first in document {first.doc_id}, {where}. "
            "A person decides whether this tender may be processed at all."
        )
        self.found = found


def code_version() -> str:
    digest = hashlib.sha256(PREP_VERSION.encode())
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    digest.update(Path(models.__file__).read_bytes())
    for lib in _LIBRARIES:
        digest.update(f"{lib}={importlib.metadata.version(lib)}".encode())
    return f"{PREP_VERSION}-{digest.hexdigest()[:12]}"


def read_text(data: bytes, builder: DocBuilder) -> None:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    para: list[str] = []
    start = 0
    for n, line in enumerate([*text.splitlines(), ""], 1):
        if line.strip():
            if not para:
                start = n
            para.append(line)
        elif para:
            builder.add(Piece("paragraph", " ".join(para), line=start))
            para = []


def read_image(data: bytes, builder: DocBuilder, put_blob: Callable[[bytes], str]) -> tuple[list[PageInfo], list[Tile]]:
    """A scanned page delivered as an image: tiled at its own resolution."""
    with Image.open(io.BytesIO(data)) as img:
        if img.width * img.height > MAX_IMAGE_PIXELS:
            raise PrepError("an image is larger than the pixel limit")
        image = img.convert("RGB")
    tiles = []
    for k, (x0, y0, x1, y1) in enumerate(tile_grid(image.width, image.height), 1):
        buf = io.BytesIO()
        box = (x0, y0, min(x1, image.width), min(y1, image.height))
        image.crop(box).save(buf, format="PNG", compress_level=6)
        tiles.append(
            Tile(
                id=f"{builder.doc_id[:6]}-p1-t{k}",
                doc_id=builder.doc_id,
                page=1,
                index=k,
                reason="scan",
                dpi=0,
                x0=box[0],
                y0=box[1],
                x1=box[2],
                y1=box[3],
                blob=put_blob(buf.getvalue()),
            )
        )
    page = PageInfo(page=1, width=image.width, height=image.height, kind="scan", chars=0, tiles=len(tiles))
    return [page], tiles


_UNREADABLE = (
    pdfium.PdfiumError,
    zipfile.BadZipFile,
    KeyError,
    ParseError,
    DefusedXmlException,
    InvalidFileException,
    UnidentifiedImageError,
    Image.DecompressionBombError,
    PrepError,
)


def prepare_file(src: Source, store: RunStore, version: str | None = None) -> tuple[DocPrep, bool]:
    """The file's prep result and whether it came from the cache."""
    version = version or code_version()
    cache = store.dir / "prep-cache" / version / src.sha256[:2] / f"{src.sha256}.json"
    if cache.exists():
        try:
            cached = DocPrep.model_validate_json(cache.read_bytes())
            for tile in cached.tiles:
                store.get_blob(tile.blob)
            return cached, True
        except (OSError, ValueError, StoreError):
            pass  # a damaged entry or a missing tile is prepared again
    builder = DocBuilder(src.sha256, src.doc_id)
    pages: list[PageInfo] = []
    sheets = []
    tiles: list[Tile] = []
    dropped = 0
    skipped = None
    try:
        if src.kind == "pdf":
            pages, tiles, dropped = read_pdf(src.data, builder, store.put_blob)
        elif src.kind == "xlsx":
            sheets = read_xlsx(src.data, builder)
        elif src.kind == "docx":
            read_docx(src.data, builder)
        elif src.kind == "text":
            read_text(src.data, builder)
        elif src.kind == "image":
            pages, tiles = read_image(src.data, builder, store.put_blob)
        else:
            skipped = f"{src.kind} files aren't read yet"
    except _UNREADABLE:
        builder = DocBuilder(src.sha256, src.doc_id)
        pages, sheets, tiles, dropped = [], [], [], 0
        skipped = "unreadable, damaged or password-protected"
    result = DocPrep(
        version=version,
        doc_id=src.doc_id,
        sha256=src.sha256,
        kind=src.kind,
        size=src.size,
        skipped=skipped,
        pages=pages,
        sheets=sheets,
        blocks=builder.blocks,
        outline=builder.outline,
        tiles=tiles,
        masked=builder.masked,
        markings=builder.markings,
        furniture_dropped=dropped,
    )
    store.write_file(cache, result.model_dump_json().encode())
    return result, False


def _jsonl(items: list[Any]) -> bytes:
    return b"".join(item.model_dump_json().encode() + b"\n" for item in items)


def summarise(docs: list[DocPrep], loaded: Loaded, cache_hits: int) -> dict[str, Any]:
    """Counts only: safe to print and to log."""
    blocks = [b for d in docs for b in d.blocks]
    pages = [p for d in docs for p in d.pages]
    return {
        "documents": len(docs),
        "documents_by_kind": dict(Counter(d.kind for d in docs)),
        "skipped": sum(d.skipped is not None for d in docs),
        "pages_by_kind": dict(Counter(p.kind for p in pages)),
        "sheets": sum(len(d.sheets) for d in docs),
        "blocks_by_kind": dict(Counter(b.kind for b in blocks)),
        "hidden_blocks": sum(b.hidden for b in blocks),
        "block_chars": sum(len(b.text) for b in blocks),
        "outline_entries": sum(len(d.outline) for d in docs),
        "tiles": sum(len(d.tiles) for d in docs),
        "masked_by_kind": dict(Counter(m.kind for d in docs for m in d.masked)),
        "furniture_dropped": sum(d.furniture_dropped for d in docs),
        "cache_hits": cache_hits,
        "links_skipped": loaded.skipped_links,
        "hidden_files_skipped": loaded.skipped_hidden,
    }


def prepare(
    company_id: str, inbox: Path, out_dir: Path, manifest: Path | None = None, root: Path | None = None
) -> dict[str, Any]:
    store = RunStore(company_id, root)
    loaded = load_folder(inbox, manifest)
    version = code_version()
    results = [prepare_file(src, store, version) for src in loaded.sources]
    docs = [doc for doc, _ in results]
    found = [m for d in docs for m in d.markings]
    if found:
        raise IntakeStop(found)
    prefixes = Counter(d.doc_id[:DOC_PREFIX] for d in docs)
    if any(n > 1 for n in prefixes.values()):
        raise PrepError("two files share a block-id prefix; block ids would collide")
    summary = summarise(docs, loaded, sum(hit for _, hit in results))
    documents = []
    for src, doc in zip(loaded.sources, docs, strict=True):
        documents.append(
            {
                "doc_id": doc.doc_id,
                "sha256": doc.sha256,
                "kind": doc.kind,
                "size": doc.size,
                "paths": list(src.paths),
                "skipped": doc.skipped,
                "pages": [p.model_dump() for p in doc.pages],
                "sheets": [s.model_dump() for s in doc.sheets],
                "blocks": len(doc.blocks),
                "tiles": len(doc.tiles),
                "masked": len(doc.masked),
            }
        )
    store.write_file(out_dir / "documents.json", canonical(documents))
    store.write_file(out_dir / "blocks.jsonl", _jsonl([b for d in docs for b in d.blocks]))
    store.write_file(out_dir / "outline.json", canonical([o.model_dump() for d in docs for o in d.outline]))
    store.write_file(out_dir / "tiles.json", canonical([t.model_dump() for d in docs for t in d.tiles]))
    store.write_file(out_dir / "masking.jsonl", _jsonl([m for d in docs for m in d.masked]))
    store.write_file(out_dir / "summary.json", json.dumps(summary, indent=2, sort_keys=True).encode())
    return summary
