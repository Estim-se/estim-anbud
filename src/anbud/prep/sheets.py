"""Spreadsheets: one block per row, with hidden state kept.

- A row's visible cells form one block, joined by ` | ` in column order. Cells in hidden columns form a second block
  for the same row, marked hidden, so a reviewer sees what the sender hid without it blending into what they showed.
- Rows that are hidden, and every row of a hidden or very hidden sheet, are marked hidden.
- Values are the cached results Excel saved (formulas aren't evaluated). Dates become ISO dates; whole floats lose
  their `.0`.
- Merged cells keep their value in the top-left cell only, as Excel stores them.
- Each sheet is an outline entry.
"""

from __future__ import annotations

import datetime as dt
import io
import re

import openpyxl
from openpyxl.utils import get_column_letter

from anbud.prep.build import DocBuilder, Piece
from anbud.prep.masking import NAME_HEADER, Cell
from anbud.prep.model import OutlineEntry, SheetInfo
from anbud.prep.text import clean

HEADER_SCAN_ROWS = 15


def _format(value: object) -> tuple[str, bool]:
    """The cell's text, and whether it is a number, date or boolean (never masked)."""
    if isinstance(value, bool):
        return ("TRUE" if value else "FALSE"), True
    if isinstance(value, int):
        return str(value), True
    if isinstance(value, float):
        return (str(int(value)) if value.is_integer() and abs(value) < 1e15 else repr(value)), True
    if isinstance(value, dt.datetime):
        return (value.date().isoformat() if value.time() == dt.time() else value.isoformat(sep=" ")), True
    if isinstance(value, dt.date | dt.time):
        return value.isoformat(), True
    if isinstance(value, dt.timedelta):
        return str(value), True
    return str(value), False


def _hidden_columns(ws: openpyxl.worksheet.worksheet.Worksheet) -> set[int]:
    hidden = set()
    for dim in ws.column_dimensions.values():
        if dim.hidden and dim.min and dim.max:
            hidden.update(range(dim.min, dim.max + 1))
    return hidden


def read_xlsx(data: bytes, builder: DocBuilder) -> list[SheetInfo]:
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=False, keep_links=False)
    infos: list[SheetInfo] = []
    try:
        for sheet in wb.chartsheets:
            infos.append(
                SheetInfo(
                    name=builder.mask(sheet.title, "sheet_name"),
                    state="chart",
                    rows=0,
                    hidden_rows=0,
                    hidden_cols=0,
                    merged=0,
                )
            )
        for ws in wb.worksheets:
            infos.append(_read_sheet(ws, builder))
    finally:
        wb.close()
    return infos


_HF_CODES = re.compile(r'&"[^"]*"|&K[0-9A-Fa-f]{6}|&\d+|&\[[^\]]*\]|&[A-Za-z]')


def _header_footer_texts(ws: openpyxl.worksheet.worksheet.Worksheet) -> list[str]:
    """The sheet's print header and footer texts, formatting codes removed, each distinct text once."""
    texts: list[str] = []
    for attr in ("oddHeader", "oddFooter", "evenHeader", "evenFooter", "firstHeader", "firstFooter"):
        part = getattr(ws, attr, None)
        if part is None:
            continue
        for side in (part.left, part.center, part.right):
            text = clean(_HF_CODES.sub(" ", side.text or "")) if side is not None else ""
            if text and text not in texts:
                texts.append(text)
    return texts


def _read_sheet(ws: openpyxl.worksheet.worksheet.Worksheet, builder: DocBuilder) -> SheetInfo:
    name = builder.mask(ws.title, "sheet_name")
    sheet_hidden = ws.sheet_state != "visible"
    hidden_cols = _hidden_columns(ws)
    name_cols: set[int] = set()
    header_row = None
    rows = hidden_rows = 0
    first_block = None
    for row in ws.iter_rows():
        filled = [c for c in row if c.value is not None and str(c.value).strip() != ""]
        if not filled:
            continue
        r = filled[0].row
        if header_row is None and r <= HEADER_SCAN_ROWS:
            heads = {c.column for c in filled if isinstance(c.value, str) and NAME_HEADER.search(c.value)}
            if heads:
                name_cols, header_row = heads, r
        row_hidden = bool(ws.row_dimensions[r].hidden) if r in ws.row_dimensions else False
        rows += 1
        hidden_rows += row_hidden
        shown, tucked = [], []
        for c in filled:
            text, numeric = _format(c.value)
            text = clean(text)
            if not text:
                continue
            in_names = c.column in name_cols and header_row is not None and r > header_row and not numeric
            cell = Cell(get_column_letter(c.column), text, numeric=numeric, name_column=in_names)
            (tucked if c.column in hidden_cols else shown).append(cell)
        for cells, hidden in ((shown, sheet_hidden or row_hidden), (tucked, True)):
            if not cells:
                continue
            raw = " | ".join(c.text for c in cells)
            block = builder.add(Piece("sheet_row", raw, sheet=name, row=r, hidden=hidden, cells=cells))
            first_block = first_block or block
    for text in _header_footer_texts(ws):
        block = builder.add(Piece("paragraph", text, sheet=name, hidden=sheet_hidden))
        first_block = first_block or block
    builder.outline.append(
        OutlineEntry(
            doc_id=builder.doc_id, level=1, title=name, block_id=first_block.id if first_block else None, sheet=name
        )
    )
    return SheetInfo(
        name=name,
        state=ws.sheet_state,
        rows=rows,
        hidden_rows=hidden_rows,
        hidden_cols=len(hidden_cols),
        merged=len(ws.merged_cells.ranges),
    )
