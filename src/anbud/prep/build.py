"""From raw pieces to citable blocks: ids from the unmasked text, then the gate's markings, then masking."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from anbud.models import Block, Box
from anbud.prep.masking import SEP, Cell, Masker, Span, markings
from anbud.prep.model import CellText, Marking, MaskedSpan, OutlineEntry, RowCells
from anbud.prep.text import BlockIds, clean

_NUMBER = re.compile(r"[-+]?\d{1,3}(?:[ \u00a0]\d{3})+(?:[.,]\d+)?|[-+]?\d+(?:[.,]\d+)?")


def _numeric(text: str) -> bool:
    return bool(_NUMBER.fullmatch(text.strip()))


@dataclass
class Piece:
    kind: str
    text: str
    page: int | None = None
    line: int | None = None
    sheet: str | None = None
    row: int | None = None
    box: Box | None = None
    hidden: bool = False
    cells: list[Cell] | None = None
    heading_level: int | None = None
    heading_number: str | None = None


@dataclass
class DocBuilder:
    sha256: str
    doc_id: str
    blocks: list[Block] = field(default_factory=list)
    masked: list[MaskedSpan] = field(default_factory=list)
    markings: list[Marking] = field(default_factory=list)
    outline: list[OutlineEntry] = field(default_factory=list)
    cells: list[RowCells] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._ids = BlockIds(self.sha256)
        self._masker = Masker()

    def add(self, piece: Piece) -> Block | None:
        raw = clean(piece.text)
        if not raw:
            return None
        block_id = self._ids.next(raw)
        self.markings += [Marking(doc_id=self.doc_id, block_id=block_id, kind=k) for k in markings(raw)]
        row_cells = None
        if piece.cells is not None:
            parts, spans = self._masker.cell_parts(piece.cells)
            text = SEP.join(parts)
            row_cells = [
                CellText(col=c.col, text=t, numeric=c.numeric) for c, t in zip(piece.cells, parts, strict=True)
            ]
        else:
            text, spans = self._masker.text(raw)
            if piece.kind == "table_row":  # a PDF or DOCX row: columns by position in the ` | ` split
                row_cells = [
                    CellText(col=str(i), text=t, numeric=_numeric(t)) for i, t in enumerate(text.split(SEP), 1)
                ]
        self.masked += [self._span(s, block_id, "block") for s in spans]
        block = Block(
            id=block_id,
            doc_id=self.doc_id,
            kind=piece.kind,  # type: ignore[arg-type]
            text=text,
            page=piece.page,
            line=piece.line,
            sheet=piece.sheet,
            row=piece.row,
            box=piece.box,
            hidden=piece.hidden,
        )
        self.blocks.append(block)
        if row_cells is not None:
            self.cells.append(RowCells(block_id=block_id, cells=row_cells))
        if piece.heading_level is not None:
            self.outline.append(
                OutlineEntry(
                    doc_id=self.doc_id,
                    level=piece.heading_level,
                    title=text,
                    number=piece.heading_number,
                    block_id=block_id,
                    page=piece.page,
                    sheet=piece.sheet,
                )
            )
        return block

    def mask(self, text: str, where: str) -> str:
        """Gate and mask text that isn't a block: bookmark titles and sheet names. Recorded like block text."""
        raw = clean(text)
        self.markings += [Marking(doc_id=self.doc_id, block_id=None, kind=k) for k in markings(raw)]
        masked, spans = self._masker.text(raw)
        self.masked += [self._span(s, None, where) for s in spans]
        return masked

    def _span(self, span: Span, block_id: str | None, where: str) -> MaskedSpan:
        return MaskedSpan(
            doc_id=self.doc_id,
            block_id=block_id,
            where=where,  # type: ignore[arg-type]
            kind=span.kind,
            start=span.start,
            end=span.end,
            original=span.original,
        )
