"""PDF: text with boxes from PDFium, paragraphs and headings by layout, tiles for scans and drawings.

- **Lines** come from PDFium's character stream, split where PDFium breaks lines or the baseline jumps. A gap wider
  than 2.5 font sizes inside a line is a column gap: it becomes ` | ` and the line is a table row.
- **Paragraphs** join consecutive lines of the same size, in the same column, with no more than half a font size of
  space between them. A bullet or list marker starts a new paragraph.
- **Headings** are short lines set larger than the body text, short bold lines, or short lines that start with a
  multi-level number or a dotted code. Bookmarks, when the file has them, give the outline instead.
- **Coordinates** are display coordinates: a page's /Rotate and crop box are applied to every character box, so a
  landscape drawing stored as a rotated portrait page reads left to right, and boxes line up with the rendered tiles.
- **Page furniture** (a line in the top or bottom band that repeats on at least half the pages, with only the page
  number and page count ignored) is kept once, on its first page, so a header with the revision survives but isn't
  repeated. A title block whose drawing number changes from page to page is not furniture.
- **Pages** are text, mixed (text plus an image covering half the page), scan (little or no text, mostly image),
  drawing (vector work with little text, or A3 and larger with heavy vector or image content) or blank (no characters
  at all). Any characters a page has become blocks, so a short stamp still reaches the gate.
- **Soft hyphens** (PDFium's end-of-line hyphen) join the word across the line break without a space.
- **Masked text on tiles**: personal data the masking finds in a page's text layer is painted over with a black box
  before a mixed or drawing page is tiled, so the image a model sees carries no more than the masked text does. A scan
  has no reliable text layer; its tiles are sent as they are, and what a page card transcribes from them is masked.
- **Tiles** cover scan, drawing and mixed pages: rendered at the embedded image's own resolution (150-300 dpi, 200 for
  vector pages), lowered by up to 10% when that saves a row or column, then cut into tiles of at most 2,576 px a side
  with at least 10% overlap.
"""

from __future__ import annotations

import ctypes
import io
import math
import re
import statistics
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from PIL import ImageDraw

from anbud.models import Box
from anbud.prep.build import DocBuilder, Piece
from anbud.prep.masking import find_spans
from anbud.prep.model import OutlineEntry, PageInfo, PageKind, Tile
from anbud.prep.text import heading_number, normalise

A4_AREA = 595.0 * 842.0
TILE_MAX = 2576
TILE_OVERLAP = 0.10
DPI_RANGE = (150, 300)
VECTOR_DPI = 200
DPI_SLACK = 0.10
MAX_RENDER_PIXELS = 90_000_000
MIN_TEXT_CHARS = 20
COLUMN_GAP = 2.5
PARAGRAPH_GAP = 0.5
MAX_PARAGRAPH = 2000
FURNITURE_BAND = 0.15
FURNITURE_DRIFT = 12.0
MAX_HEADING = 150
BOLD_STEP = 200
SOFT_HYPHEN = "\u00ad"
MASK_PAD = 1.0  # points around a masked glyph box
BULLET = re.compile(r"^(?:[•●▪■◦○·\-–*]|\(?[0-9]{1,2}[.)]|\(?[a-zåäö][.)])\s")
_BOLD_NAMES = (b"bold", b"black", b"heavy", b"semibold", b"demi")


@dataclass
class _Glyph:
    ch: str
    x0: float
    top: float
    x1: float
    bottom: float
    size: float
    weight: int
    named_bold: bool


_SPACE = _Glyph(" ", 0, 0, 0, 0, 0, 0, False)


@dataclass
class _Line:
    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    size: float
    weight: int
    named_bold: bool
    table: bool
    page: int
    index: int
    owners: list[_Glyph | None] = field(default_factory=list, repr=False)  # the glyph behind each character of text
    bold: bool = False

    @property
    def box(self) -> Box:
        return Box(x0=round(self.x0, 1), y0=round(self.top, 1), x1=round(self.x1, 1), y1=round(self.bottom, 1))


@dataclass
class _Para:
    lines: list[_Line] = field(default_factory=list)

    @property
    def last(self) -> _Line:
        return self.lines[-1]

    @property
    def text(self) -> str:
        return self.text_and_owners()[0]

    def text_and_owners(self) -> tuple[str, list[_Glyph | None]]:
        """Lines joined by a space, or by nothing after a soft hyphen (which is dropped)."""
        text = ""
        owners: list[_Glyph | None] = []
        for line in self.lines:
            if text.endswith(SOFT_HYPHEN):
                text, owners = text[:-1], owners[:-1]
            elif text:
                text += " "
                owners.append(None)
            text += line.text
            owners += line.owners
        return text, owners

    @property
    def box(self) -> Box:
        return Box(
            x0=round(min(ln.x0 for ln in self.lines), 1),
            y0=round(min(ln.top for ln in self.lines), 1),
            x1=round(max(ln.x1 for ln in self.lines), 1),
            y1=round(max(ln.bottom for ln in self.lines), 1),
        )


Transform = Callable[[float, float, float, float], tuple[float, float, float, float]]


def display_transform(page: pdfium.PdfPage) -> Transform:
    """Maps a PDF-space box (left, bottom, right, top) to display coordinates (x0, top, x1, bottom), measured in points
    from the top-left corner of the page as rendered: crop box origin and /Rotate applied."""
    rotation = page.get_rotation() % 360
    left, bottom, right, top = page.get_cropbox()
    w, h = right - left, top - bottom

    def point(x: float, y: float) -> tuple[float, float]:
        u, v = x - left, y - bottom
        if rotation == 90:
            return v, u
        if rotation == 180:
            return w - u, v
        if rotation == 270:
            return h - v, w - u
        return u, h - v

    def box(x0: float, y0: float, x1: float, y1: float) -> tuple[float, float, float, float]:
        (ax, ay), (bx, by) = point(x0, y0), point(x1, y1)
        return min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)

    return box


def _glyphs(textpage: pdfium.PdfTextPage, to_display: Transform) -> list[_Glyph | None]:
    """The page's characters in PDFium's order; None where PDFium breaks a line."""
    raw = textpage.raw
    name = ctypes.create_string_buffer(256)
    flags = ctypes.c_int()
    out: list[_Glyph | None] = []
    for i in range(textpage.count_chars()):
        code = pdfium_c.FPDFText_GetUnicode(raw, i)
        if code in (0x0A, 0x0D):
            out.append(None)
            continue
        if code == 0 or 0xD800 <= code <= 0xDFFF:
            continue
        ch = SOFT_HYPHEN if code in (0x02, 0xFFFE) else chr(code)
        if ch.isspace():
            out.append(_SPACE)
            continue
        try:
            left, bottom, right, top = textpage.get_charbox(i, loose=True)
        except pdfium.PdfiumError:
            continue
        weight = pdfium_c.FPDFText_GetFontWeight(raw, i)
        pdfium_c.FPDFText_GetFontInfo(raw, i, name, len(name), ctypes.byref(flags))
        named_bold = any(b in name.value.lower() for b in _BOLD_NAMES)
        size = pdfium_c.FPDFText_GetFontSize(raw, i)
        x0, y0, x1, y1 = to_display(left, bottom, right, top)
        out.append(_Glyph(ch, x0, y0, x1, y1, size, weight, named_bold))
    return out


def _line_text(glyphs: list[_Glyph]) -> tuple[str, list[_Glyph | None], bool]:
    """The line's text, the glyph behind each character (None for inserted spaces and separators), and whether it
    has a column gap."""
    parts: list[str] = []
    owners: list[_Glyph | None] = []
    prev: _Glyph | None = None
    space = table = False
    for g in glyphs:
        if g is _SPACE:
            space = True
            continue
        if prev is not None:
            size = max(g.size, prev.size, 1.0)
            gap = g.x0 - prev.x1
            if gap > COLUMN_GAP * size:
                parts.append(" | ")
                owners += [None, None, None]
                table = True
            elif space or gap > 0.25 * size:
                parts.append(" ")
                owners.append(None)
        parts.append(g.ch)
        owners.append(g)
        prev, space = g, False
    return "".join(parts), owners, table


def _lines(glyphs: list[_Glyph | None], page: int, split_columns: bool = False) -> list[_Line]:
    """Lines in PDFium's order. With `split_columns` (drawings), a column gap ends the line, so each label on a
    drawing is its own line with its own box."""
    lines: list[_Line] = []
    current: list[_Glyph] = []

    def flush() -> None:
        marks = [g for g in current if g is not _SPACE]
        if marks:
            text, owners, table = _line_text(current)
            lead = len(text) - len(text.lstrip())
            text = text.strip()
            sizes = [g.size for g in marks]
            lines.append(
                _Line(
                    text=text,
                    x0=min(g.x0 for g in marks),
                    top=min(g.top for g in marks),
                    x1=max(g.x1 for g in marks),
                    bottom=max(g.bottom for g in marks),
                    size=statistics.median(sizes),
                    weight=int(statistics.median(g.weight for g in marks)),
                    named_bold=sum(g.named_bold for g in marks) * 2 > len(marks),
                    table=table,
                    page=page,
                    index=len(lines) + 1,
                    owners=owners[lead : lead + len(text)],
                )
            )
        current.clear()

    last: _Glyph | None = None
    for g in glyphs:
        if g is None:
            flush()
            last = None
            continue
        if g is not _SPACE and last is not None:
            size = max(g.size, last.size, 1.0)
            jump = abs((g.top + g.bottom) - (last.top + last.bottom)) / 2
            if jump > 0.7 * size or (split_columns and g.x0 - last.x1 > COLUMN_GAP * size):
                flush()
        current.append(g)
        if g is not _SPACE:
            last = g
    flush()
    return lines


@dataclass
class _PageScan:
    info: PageInfo
    lines: list[_Line]
    native_dpi: float | None


def _classify(page: pdfium.PdfPage, chars: int, map_errors: int) -> tuple[PageKind, float | None]:
    width, height = page.get_size()
    area = width * height
    paths = 0
    image_area = 0.0
    native: list[float] = []
    for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_PATH, pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=15):
        if obj.type == pdfium_c.FPDF_PAGEOBJ_PATH:
            paths += 1
            continue
        left, bottom, right, top = obj.get_bounds()
        w, h = max(right - left, 0.0), max(top - bottom, 0.0)
        image_area += w * h
        if w * h > 0.25 * area and w > 0:
            px_w, _ = obj.get_px_size()
            native.append(px_w / (w / 72.0))
    cover = min(image_area / area, 1.0) if area else 0.0
    dpi = max(native) if native else None
    large = area >= 1.4 * A4_AREA
    unreliable = _unreliable(chars, map_errors)
    if chars < MIN_TEXT_CHARS or unreliable:
        if cover >= 0.5:
            return "scan", dpi
        if paths >= 50:
            return "drawing", dpi
        if unreliable:
            return "scan", dpi
        return ("text" if chars else "blank"), dpi
    if large and (paths >= 200 or cover >= 0.5):
        return "drawing", dpi
    if cover >= 0.5:
        return "mixed", dpi
    return "text", dpi


def _unreliable(chars: int, map_errors: int) -> bool:
    return bool(chars) and map_errors > 0.3 * chars


def _scan_page(pdf: pdfium.PdfDocument, index: int) -> _PageScan:
    page = pdf[index]
    width, height = page.get_size()
    textpage = page.get_textpage()
    glyphs = _glyphs(textpage, display_transform(page))
    chars = sum(1 for g in glyphs if g is not None and g is not _SPACE)
    errors = sum(1 for i in range(textpage.count_chars()) if pdfium_c.FPDFText_HasUnicodeMapError(textpage.raw, i) == 1)
    kind, dpi = _classify(page, chars, errors)
    # A scan's few characters (a stamp, a page number) still become blocks; a broken text layer does not.
    lines = [] if _unreliable(chars, errors) else _lines(glyphs, index + 1, split_columns=kind == "drawing")
    info = PageInfo(page=index + 1, width=round(width, 1), height=round(height, 1), kind=kind, chars=chars)
    return _PageScan(info, lines, dpi)


def _furniture(scans: list[_PageScan]) -> set[int]:
    """ids of furniture lines to drop: every repeat after the first page a header or footer appears on.

    A repeat has the same text with digits ignored, in the top or bottom band, at the same height give or take
    FURNITURE_DRIFT points."""
    seen: dict[tuple[str, int], set[int]] = {}
    band_lines: list[tuple[tuple[str, int], _Line]] = []
    total = len(scans)
    for scan in scans:
        band = FURNITURE_BAND * scan.info.height
        page_numbers = {scan.info.page, total}

        def wildcard(m: re.Match[str], numbers: set[int] = page_numbers) -> str:
            return "#" if int(m.group()) in numbers else m.group()

        for line in scan.lines:
            if line.top < band or line.bottom > scan.info.height - band:
                key = (re.sub(r"\d+", wildcard, normalise(line.text)), round(line.top / FURNITURE_DRIFT))
                seen.setdefault(key, set()).add(line.page)
                band_lines.append((key, line))
    threshold = max(3, math.ceil(0.5 * len(scans)))
    repeated = {k for k, pages in seen.items() if len(pages) >= threshold}
    first: dict[tuple[str, int], int] = {}
    drop: set[int] = set()
    for key, line in band_lines:
        if key in repeated:
            if first.setdefault(key, line.page) != line.page:
                drop.add(id(line))
    return drop


def _half(size: float) -> float:
    return round(size * 2) / 2


def _body_size(lines: list[_Line]) -> float:
    weights: Counter[float] = Counter()
    for line in lines:
        weights[_half(line.size)] += len(line.text)
    return weights.most_common(1)[0][0] if weights else 10.0


def _set_bold(lines: list[_Line]) -> None:
    """Bold by font name, or by a weight clearly above the body's: some producers give the regular face 700."""
    weights: Counter[int] = Counter()
    for line in lines:
        if line.weight > 0:
            weights[line.weight] += len(line.text)
    body = weights.most_common(1)[0][0] if weights else 0
    for line in lines:
        line.bold = line.named_bold or (body > 0 and line.weight >= body + BOLD_STEP)


def _is_heading(para: _Para, body: float) -> bool:
    """One or two lines, short, not ending like a sentence, and set larger, bold or numbered."""
    text = para.text
    first = para.lines[0]
    if len(para.lines) > 2 or first.table or len(text) > MAX_HEADING or not any(c.isalpha() for c in text):
        return False
    if text.endswith((".", ",", ";", ":")) or BULLET.match(text):
        return False
    if first.size >= body * 1.15 or first.bold:
        return True
    number, _ = heading_number(text)
    return number is not None and "." in number and len(text) <= 90


def _demote_label_runs(paras: list[_Para], body: float) -> list[_Para]:
    """The page's paragraphs minus runs of three or more bold-only headings in a row (no larger size, no number):
    those are table or form labels, not headings."""
    keep: list[_Para] = []
    run: list[_Para] = []

    def bold_only(para: _Para) -> bool:
        first = para.lines[0]
        return _is_heading(para, body) and first.size < body * 1.15 and heading_number(para.text)[0] is None

    for para in [*paras, None]:
        if para is not None and bold_only(para):
            run.append(para)
            continue
        if len(run) < 3:
            keep += run
        run = []
        if para is not None:
            keep.append(para)
    return keep


def _paragraphs(lines: list[_Line]) -> list[_Para]:
    paras: list[_Para] = []
    for line in lines:
        prev = paras[-1] if paras else None
        if prev is not None and _continues(prev, line):
            prev.lines.append(line)
        else:
            paras.append(_Para([line]))
    return paras


def _continues(para: _Para, line: _Line) -> bool:
    last = para.last
    if last.table or line.table or last.bold != line.bold or BULLET.match(line.text):
        return False
    if abs(line.size - last.size) > 0.6 or len(para.text) >= MAX_PARAGRAPH:
        return False
    gap = line.top - last.bottom
    if not -0.2 * line.size <= gap <= PARAGRAPH_GAP * line.size:
        return False
    return line.x0 < max(ln.x1 for ln in para.lines) and line.x1 > min(ln.x0 for ln in para.lines)


def tile_grid(width: int, height: int) -> list[tuple[int, int, int, int]]:
    """Tiles of at most TILE_MAX a side covering the image, overlapping by at least TILE_OVERLAP, evenly spaced."""

    def spans(total: int) -> list[tuple[int, int]]:
        if total <= TILE_MAX:
            return [(0, total)]
        step = TILE_MAX * (1 - TILE_OVERLAP)
        count = math.ceil((total - TILE_MAX) / step) + 1
        starts = [round(i * (total - TILE_MAX) / (count - 1)) for i in range(count)]
        return [(s, s + TILE_MAX) for s in starts]

    return [(x0, y0, x1, y1) for y0, y1 in spans(height) for x0, x1 in spans(width)]


def render_dpi(width_pt: float, height_pt: float, native: float | None) -> int:
    """The dpi to render at: the image's own (or VECTOR_DPI), clamped, capped by pixel count, then lowered by up to
    DPI_SLACK when that needs fewer tiles."""
    base = VECTOR_DPI if native is None else min(max(native, DPI_RANGE[0]), DPI_RANGE[1])
    cap = math.sqrt(MAX_RENDER_PIXELS / max(width_pt / 72 * height_pt / 72, 1e-6))
    base = int(min(base, cap))

    def count(dpi: int) -> int:
        return len(tile_grid(round(width_pt / 72 * dpi), round(height_pt / 72 * dpi)))

    floor = int(base * (1 - DPI_SLACK))
    best = base
    for dpi in range(base, floor - 1, -1):
        if count(dpi) < count(best):
            best = dpi
    return best


def _masked_boxes(texts: list[tuple[str, list[_Glyph | None]]]) -> list[tuple[float, float, float, float]]:
    """One display box per line of each span the masking finds, covering the gaps between its glyphs too."""
    boxes = []
    for text, owners in texts:
        for start, end, _kind in find_spans(text):
            runs: list[list[_Glyph]] = []
            for g in (g for g in owners[start:end] if g is not None):
                if runs and abs(g.top - runs[-1][-1].top) < 0.5 * max(g.size, 1.0):
                    runs[-1].append(g)
                else:
                    runs.append([g])
            for run in runs:
                boxes.append(
                    (
                        min(g.x0 for g in run),
                        min(g.top for g in run),
                        max(g.x1 for g in run),
                        max(g.bottom for g in run),
                    )
                )
    return boxes


def _tiles(
    page: pdfium.PdfPage,
    info: PageInfo,
    doc_id: str,
    reason: str,
    native: float | None,
    put_blob: Callable[[bytes], str],
    masked: list[tuple[float, float, float, float]] | None = None,
) -> list[Tile]:
    dpi = render_dpi(info.width, info.height, native)
    image = page.render(scale=dpi / 72).to_pil().convert("RGB")
    if masked:
        draw = ImageDraw.Draw(image)
        scale = dpi / 72
        for x0, y0, x1, y1 in masked:
            draw.rectangle(
                [(x0 - MASK_PAD) * scale, (y0 - MASK_PAD) * scale, (x1 + MASK_PAD) * scale, (y1 + MASK_PAD) * scale],
                fill=(0, 0, 0),
            )
    tiles = []
    for k, (x0, y0, x1, y1) in enumerate(tile_grid(*image.size), 1):
        buf = io.BytesIO()
        image.crop((x0, y0, min(x1, image.width), min(y1, image.height))).save(buf, format="PNG", compress_level=6)
        tiles.append(
            Tile(
                id=f"{doc_id[:6]}-p{info.page}-t{k}",
                doc_id=doc_id,
                page=info.page,
                index=k,
                reason=reason,  # type: ignore[arg-type]
                dpi=dpi,
                x0=x0,
                y0=y0,
                x1=min(x1, image.width),
                y1=min(y1, image.height),
                blob=put_blob(buf.getvalue()),
            )
        )
    return tiles


def _bookmarks(pdf: pdfium.PdfDocument, builder: DocBuilder) -> list[OutlineEntry]:
    headings = [b for b in builder.blocks if b.kind == "heading"]
    entries = []
    for mark in pdf.get_toc():
        dest = mark.get_dest()
        page = dest.get_index() + 1 if dest is not None and dest.get_index() is not None else None
        title = builder.mask(mark.get_title(), "outline")
        wanted = normalise(title)
        match = next(
            (b.id for b in headings if b.page == page and wanted and wanted in normalise(b.text)),
            None,
        )
        number, _ = heading_number(title)
        entries.append(
            OutlineEntry(
                doc_id=builder.doc_id, level=mark.level + 1, title=title, number=number, block_id=match, page=page
            )
        )
    return entries


def read_pdf(
    data: bytes, builder: DocBuilder, put_blob: Callable[[bytes], str]
) -> tuple[list[PageInfo], list[Tile], int]:
    """Blocks and outline go into the builder; returns page info, tiles and how many furniture lines were dropped."""
    pdf = pdfium.PdfDocument(data)
    try:
        scans = [_scan_page(pdf, i) for i in range(len(pdf))]
        drop = _furniture(scans)
        kept = {s.info.page: [ln for ln in s.lines if id(ln) not in drop] for s in scans}
        _set_bold([ln for lines in kept.values() for ln in lines])
        text_pages = [s.info.page for s in scans if s.info.kind != "drawing" and kept[s.info.page]]
        body = _body_size([ln for page in text_pages for ln in kept[page]])
        paras = {page: _paragraphs(kept[page]) for page in text_pages}
        headings = {id(p) for page in text_pages for p in _demote_label_runs(paras[page], body) if _is_heading(p, body)}
        sizes = sorted(
            {_half(p.lines[0].size) for page in text_pages for p in paras[page] if id(p) in headings}, reverse=True
        )
        for scan in scans:
            page = scan.info.page
            if scan.info.kind == "drawing":
                for line in kept[page]:  # labels on a drawing: one block each, never joined
                    builder.add(Piece("paragraph", line.text, page=page, line=line.index, box=line.box))
                continue
            for para in paras.get(page, []):
                first = para.lines[0]
                if id(para) in headings:
                    number, level = heading_number(para.text)
                    if level is None:
                        level = sizes.index(_half(first.size)) + 1
                    builder.add(
                        Piece(
                            "heading",
                            para.text,
                            page=page,
                            line=first.index,
                            box=para.box,
                            heading_level=level,
                            heading_number=number,
                        )
                    )
                else:
                    kind = "table_row" if first.table else "paragraph"
                    builder.add(Piece(kind, para.text, page=page, line=first.index, box=para.box))
        if any(True for _ in pdf.get_toc()):
            builder.outline = _bookmarks(pdf, builder)
        pages, tiles = [], []
        for scan in scans:
            info = scan.info
            if info.kind in ("scan", "drawing", "mixed"):
                texts = [(line.text, line.owners) for line in scan.lines]  # furniture included: it is in the image
                texts += [para.text_and_owners() for para in paras.get(info.page, [])]
                masked = _masked_boxes(texts)
                page_tiles = _tiles(
                    pdf[info.page - 1], info, builder.doc_id, info.kind, scan.native_dpi, put_blob, masked
                )
                tiles += page_tiles
                info = info.model_copy(update={"tiles": len(page_tiles)})
            pages.append(info)
        return pages, tiles, len(drop)
    finally:
        pdf.close()
