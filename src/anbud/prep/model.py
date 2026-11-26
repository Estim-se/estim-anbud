"""What document preparation produces for one file, cached per company by the file's content hash.

Boxes are in PDF points with the origin at the top left of the page, y growing downwards. Tile boxes are in pixels of
the page rendered at the tile's dpi, same orientation.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from anbud.models import Block

DocKind = Literal["pdf", "xlsx", "docx", "text", "html", "image", "dwg", "ifc", "legacy_office", "archive", "other"]
PageKind = Literal["text", "mixed", "scan", "drawing", "blank"]
MaskKind = Literal["personnummer", "email", "iban", "phone", "name"]
MarkingKind = Literal["hemlig", "sakerhetsskydd"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PageInfo(_Model):
    page: int
    width: float
    height: float
    kind: PageKind
    chars: int
    tiles: int = 0


class SheetInfo(_Model):
    name: str
    state: Literal["visible", "hidden", "veryHidden", "chart"]
    rows: int
    hidden_rows: int
    hidden_cols: int
    merged: int


class Tile(_Model):
    id: str
    doc_id: str
    page: int
    index: int
    reason: Literal["scan", "drawing", "mixed"]
    dpi: int
    x0: int
    y0: int
    x1: int
    y1: int
    blob: str


class OutlineEntry(_Model):
    doc_id: str
    level: int
    title: str
    number: str | None = None
    block_id: str | None = None
    page: int | None = None
    sheet: str | None = None


class MaskedSpan(_Model):
    """One masked span. `start` and `end` locate the mask token in the masked text of the block, or of the outline
    title or sheet name when `where` says so (then `block_id` is None)."""

    doc_id: str
    block_id: str | None
    where: Literal["block", "outline", "sheet_name"] = "block"
    kind: MaskKind
    start: int
    end: int
    original: str


class Marking(_Model):
    doc_id: str
    block_id: str | None
    kind: MarkingKind


class CellText(_Model):
    """One masked cell of a row: its column (a sheet's letter, or a PDF table row's position from 1) and text."""

    col: str
    text: str
    numeric: bool = False


class RowCells(_Model):
    """The cells of one sheet_row or table_row block, for code that reads tables (`prep/cells.jsonl`). Joined with
    ` | ` they are the block's text."""

    block_id: str
    cells: list[CellText]


class DocPrep(_Model):
    version: str
    doc_id: str
    sha256: str
    kind: DocKind
    size: int
    skipped: str | None = None
    pages: list[PageInfo] = []
    sheets: list[SheetInfo] = []
    blocks: list[Block] = []
    outline: list[OutlineEntry] = []
    tiles: list[Tile] = []
    masked: list[MaskedSpan] = []
    markings: list[Marking] = []
    furniture_dropped: int = 0
    withheld: int = 0  # answer-key sections of a web page left out (anbud.answer_terms)
    cells: list[RowCells] = []
