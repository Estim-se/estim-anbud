"""`anbud prep`: a tender's files to blocks, an outline, tiles and a masking report.

Each file's result is cached in the company's folder under its content hash and a hash of the prep code and library
versions. Re-running is free, and any code change prepares every file again. Tiles are PNGs in the company's blob
store. For the tender, prep writes into its `prep/` folder:

    documents.json   one entry per file: id, kind, size, pages, sheets, counts, and its local path(s)
    blocks.jsonl     every block, masked, in document order
    cells.jsonl      for each sheet_row and table_row block, its masked cells with their columns (a sidecar)
    outline.json     headings, bookmarks and sheets
    tiles.json       tile metadata; the PNGs are blobs
    masking.jsonl    every masked span with its original text (local, like the files themselves)
    summary.json     counts only

A security marking anywhere stops the tender before any of these are written, and removes an earlier `prep/`. A
marked file's tiles never reach the blob store and its result is never cached: tiles wait in a staging folder until
the file has passed the gate. Outputs are written to a sibling folder and swapped in, so a failed run never leaves a
mix of old and new files.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import io
import json
import os
import shutil
import zipfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import ParseError

import pypdfium2 as pdfium
from defusedxml import DefusedXmlException
from openpyxl.utils.exceptions import InvalidFileException
from PIL import Image, ImageSequence, UnidentifiedImageError

from anbud import models, paths
from anbud.prep.build import DocBuilder, Piece
from anbud.prep.docx import read_docx
from anbud.prep.model import DocPrep, Marking, PageInfo, Tile
from anbud.prep.pdf import read_pdf, tile_grid
from anbud.prep.sheets import read_xlsx
from anbud.prep.sources import Loaded, Source, load_folder
from anbud.prep.text import DOC_PREFIX
from anbud.store import RunStore, StoreError, canonical

PREP_VERSION = "1"
CELLS_FILE = "cells.jsonl"
_LIBRARIES = ("pypdfium2", "openpyxl", "pillow", "defusedxml")
MAX_IMAGE_PIXELS = 120_000_000
MAX_IMAGE_PAGES = 500


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
    """Scanned pages delivered as an image, one page per frame (multi-page TIFF): tiled at their own resolution."""
    pages: list[PageInfo] = []
    tiles: list[Tile] = []
    with Image.open(io.BytesIO(data)) as img:
        for n, frame in enumerate(ImageSequence.Iterator(img), 1):
            if n > MAX_IMAGE_PAGES or frame.width * frame.height > MAX_IMAGE_PIXELS:
                raise PrepError("an image is larger than the pixel or page limit")
            image = frame.convert("RGB")
            count = 0
            for k, (x0, y0, x1, y1) in enumerate(tile_grid(image.width, image.height), 1):
                buf = io.BytesIO()
                box = (x0, y0, min(x1, image.width), min(y1, image.height))
                image.crop(box).save(buf, format="PNG", compress_level=6)
                tiles.append(
                    Tile(
                        id=f"{builder.doc_id[:6]}-p{n}-t{k}",
                        doc_id=builder.doc_id,
                        page=n,
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
                count += 1
            pages.append(PageInfo(page=n, width=image.width, height=image.height, kind="scan", chars=0, tiles=count))
    return pages, tiles


# What a damaged, odd or hostile file can raise inside the readers. A store error is never one of them: it propagates.
_UNREADABLE = (
    pdfium.PdfiumError,
    zipfile.BadZipFile,
    ParseError,
    DefusedXmlException,
    InvalidFileException,
    UnidentifiedImageError,
    Image.DecompressionBombError,
    OSError,
    ValueError,
    TypeError,
    LookupError,
    ArithmeticError,
    EOFError,
    RuntimeError,
)


class _Staging:
    """Tiles of a file still waiting for the gate, in a private folder inside the company's folder."""

    def __init__(self, store: RunStore, sha256: str) -> None:
        self.store = store
        self.dir = store.dir / "prep-staging" / sha256
        self.blobs: list[str] = []

    def put(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        self.store.write_file(self.dir / sha, data)
        self.blobs.append(sha)
        return sha

    def commit(self) -> None:
        for sha in dict.fromkeys(self.blobs):
            self.store.put_blob((self.dir / sha).read_bytes())
        self.discard()

    def discard(self) -> None:
        remove_tree(self.store, self.dir)


def remove_tree(store: RunStore, path: Path) -> None:
    """Remove a folder inside the company's folder; anything else is refused."""
    if path.is_symlink():
        raise StoreError("refusing to remove through a link")
    if not path.exists():
        return
    if not path.resolve().is_relative_to(store.dir.resolve()) or path.resolve() == store.dir.resolve():
        raise StoreError("a removal outside the company's folder")
    shutil.rmtree(path)


def prepare_file(src: Source, store: RunStore, version: str | None = None) -> tuple[DocPrep, bool]:
    """The file's prep result and whether it came from the cache.

    A file with a security marking is returned without caching it and without keeping its tiles."""
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
    staging = _Staging(store, src.sha256)
    staging.discard()  # left over from an interrupted run
    pages: list[PageInfo] = []
    sheets = []
    tiles: list[Tile] = []
    dropped = 0
    skipped = None
    try:
        if src.too_large:
            skipped = "too large"
        elif src.kind == "pdf":
            pages, tiles, dropped = read_pdf(src.data, builder, staging.put)
        elif src.kind == "xlsx":
            sheets = read_xlsx(src.data, builder)
        elif src.kind == "docx":
            read_docx(src.data, builder)
        elif src.kind == "text":
            read_text(src.data, builder)
        elif src.kind == "image":
            pages, tiles = read_image(src.data, builder, staging.put)
        else:
            skipped = f"{src.kind} files aren't read yet"
    except StoreError:
        staging.discard()
        raise
    except _UNREADABLE:
        staging.discard()
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
        cells=builder.cells,
    )
    if builder.markings:
        staging.discard()
        return result, False
    staging.commit()
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
    if any(paths.is_sealed_path(p, root) for p in (inbox, out_dir) if p is not None) or (
        manifest is not None and paths.is_sealed_path(manifest, root)
    ):
        raise PrepError("the inbox, manifest or output folder is in held-out or sealed storage")
    new = out_dir.with_name(f".{out_dir.name}.new")
    old = out_dir.with_name(f".{out_dir.name}.old")
    remove_tree(store, new)
    remove_tree(store, old)
    try:
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
        _write_outputs(store, new, loaded, docs, summary)
    except BaseException:
        remove_tree(store, new)
        remove_tree(store, out_dir)  # outputs that no longer match the inbox are worse than none
        raise
    if out_dir.exists():
        os.rename(out_dir, old)
    os.rename(new, out_dir)
    remove_tree(store, old)
    return summary


def write_cells_sidecar(
    company_id: str, inbox: Path, out_dir: Path, manifest: Path | None = None, root: Path | None = None
) -> dict[str, Any]:
    """Add `cells.jsonl` to an existing prep folder without touching anything else in it, for a tender whose blocks
    must stay as they are (a frozen run). The files are prepared again (cached per company), and the sidecar is
    written only if the blocks come out byte for byte as in the folder's `blocks.jsonl`; otherwise nothing changes."""
    store = RunStore(company_id, root)
    if any(paths.is_sealed_path(p, root) for p in (inbox, out_dir)) or (
        manifest is not None and paths.is_sealed_path(manifest, root)
    ):
        raise PrepError("the inbox, manifest or output folder is in held-out or sealed storage")
    current = out_dir / "blocks.jsonl"
    if not current.exists():
        raise PrepError("the tender has no prep output; run `anbud prep` first")
    loaded = load_folder(inbox, manifest)
    version = code_version()
    docs = [prepare_file(src, store, version)[0] for src in loaded.sources]
    if any(d.markings for d in docs):
        raise IntakeStop([m for d in docs for m in d.markings])
    if _jsonl([b for d in docs for b in d.blocks]) != current.read_bytes():
        raise PrepError("the blocks would differ from the prep folder's; run `anbud prep` for a full prep instead")
    rows = [c for d in docs for c in d.cells]
    store.write_file(out_dir / CELLS_FILE, _jsonl(rows))
    return {"rows_with_cells": len(rows), "blocks_unchanged": True}


def _write_outputs(store: RunStore, out_dir: Path, loaded: Loaded, docs: list[DocPrep], summary: dict) -> None:
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
    store.write_file(out_dir / CELLS_FILE, _jsonl([c for d in docs for c in d.cells]))
    store.write_file(out_dir / "summary.json", json.dumps(summary, indent=2, sort_keys=True).encode())
