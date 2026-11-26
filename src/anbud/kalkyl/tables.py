"""Code reads tables (#73): the model names a table's columns once, code reads every row.

The cells come from prep's sidecar `prep/cells.jsonl` (a sheet row's masked cells with their column letters, or a PDF
or DOCX table row's cells by position). Blocks are unchanged; a row without cells can't be read this way, and the
model records it one by one instead.

A layout is checked before any row is read: its header and rows lie in the chunk, in one document and sheet, the rows
have cells, every named column occurs in them, and the roles fit the table (an object table needs an id column and an
object kind; a requirement table needs a description and a quantity column). A row code can't read (no id, no
description, a quantity that isn't one number) is listed in "Ej omhändertaget" with the reason. Rows the model set
aside (headings, totals) are skipped. A row read by code is exact evidence: the citation is the whole row. A table
the model ends early is extended over the following rows of the same shape (`extend_layout`).
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from pydantic import ValidationError

from anbud.granskning.corpus import Corpus
from anbud.kalkyl.lines import Chunk
from anbud.kalkyl.records import Contained, QuantityRecord, TableLayout
from anbud.models import Citation
from anbud.prep.model import CellText, RowCells

_ROW_KINDS = ("sheet_row", "table_row")
_NUMBER_UNIT = re.compile(r"^\s*([-+]?\d{1,3}(?:[  ]\d{3})+(?:[.,]\d+)?|[-+]?\d+(?:[.,]\d+)?)\s*(\S.{0,19})?\s*$")

Cells = dict[str, list[CellText]]


def load_cells(path: Path) -> Cells:
    """The prep sidecar, or nothing if the tender was prepared before it existed."""
    if not path.exists():
        return {}
    out: Cells = {}
    for line in path.read_text().splitlines():
        if not line:
            continue
        try:
            row = RowCells.model_validate_json(line)
        except ValidationError:
            continue
        out[row.block_id] = row.cells
    return out


def number_and_unit(text: str) -> tuple[float | None, str | None]:
    """`24`, `24 st`, `1 200`, `2,5 m` -> the number and what follows it; anything else -> None."""
    m = _NUMBER_UNIT.match(text)
    if not m:
        return None, None
    return float(re.sub(r"[  ]", "", m.group(1)).replace(",", ".")), (m.group(2) or None)


def layout_rows(layout: TableLayout, chunk: Chunk) -> list[str]:
    """The rows a layout covers in this chunk, first to last; none if it doesn't fit the chunk."""
    return _rows(layout, chunk) or []


def _rows(layout: TableLayout, chunk: Chunk) -> list[str] | None:
    try:
        i, j = chunk.block_ids.index(layout.first_block_id), chunk.block_ids.index(layout.last_block_id)
    except ValueError:
        return None
    return chunk.block_ids[i : j + 1] if i <= j else None


_NUMERIC = re.compile(r"[-+]?\d[\d  .,]*")


def row_pattern(block_id: str, cells: Cells) -> tuple[tuple[str, bool], ...] | None:
    """A row's shape: its filled columns, and whether each holds a number. None for a row without cells."""
    row = cells.get(block_id)
    if not row:
        return None
    return tuple((c.col, bool(_NUMERIC.fullmatch(c.text.strip()))) for c in row if c.text.strip())


def extend_layout(
    layout: TableLayout, chunk: Chunk, corpus: Corpus, cells: Cells, taken: set[str]
) -> tuple[TableLayout, int]:
    """The layout with its last row moved forward over the rows right after it in this chunk that have the shape of
    the table's body (the shape most of its rows share, at least three), and the number of rows added.

    The model sometimes ends a table early and leaves the rest of the list neither read nor set aside. Extension stops
    at the first row of another sheet or another shape, or one the model cited or set aside (`taken`), so totals,
    notes and rows the model handled itself stay as they are."""
    rows = _rows(layout, chunk)
    if not rows:
        return layout, 0
    shapes = Counter(p for b in rows if b != layout.header_block_id and (p := row_pattern(b, cells)))
    if not shapes:
        return layout, 0
    body, n = shapes.most_common(1)[0]
    if n < 3 or n * 2 < len(rows):
        return layout, 0
    header = corpus.blocks[layout.header_block_id]
    last, added = layout.last_block_id, 0
    for block_id in chunk.block_ids[chunk.block_ids.index(layout.last_block_id) + 1 :]:
        block = corpus.blocks[block_id]
        if block.kind not in _ROW_KINDS or (block.doc_id, block.sheet) != (header.doc_id, header.sheet):
            break
        if block_id in taken or row_pattern(block_id, cells) != body:
            break
        last, added = block_id, added + 1
    return (layout.model_copy(update={"last_block_id": last}), added) if added else (layout, 0)


def check_layout(layout: TableLayout, chunk: Chunk, corpus: Corpus, cells: Cells) -> str | None:
    """Why the layout can't be read, or None."""
    if layout.header_block_id not in chunk.citable:
        return f"header block {layout.header_block_id} isn't in this chunk"
    rows = _rows(layout, chunk)
    if rows is None:
        return "first_block_id and last_block_id must be rows of this chunk, first before last"
    header = corpus.blocks[layout.header_block_id]
    places = {(corpus.blocks[b].doc_id, corpus.blocks[b].sheet) for b in rows if corpus.blocks[b].kind in _ROW_KINDS}
    if places != {(header.doc_id, header.sheet)}:
        return "the header and rows must be table rows of one document and sheet"
    with_cells = [b for b in rows if b in cells]
    if not with_cells:
        return "these rows have no cols attribute, so code can't read them; record them one by one"
    seen = {c.col for b in [*with_cells, layout.header_block_id] for c in cells.get(b, [])}
    for column in layout.columns:
        if column.col not in seen:
            return f"column {column.col} isn't in these rows"
        if column.role == "contains" and column.object_kind is None:
            return f"column {column.col}: contains needs object_kind"
    roles = [c.role for c in layout.columns if c.role != "contains"]
    if len(roles) != len(set(roles)):
        return "each role except contains names one column"
    if layout.row_kind == "object" and (layout.object_kind is None or "id" not in roles):
        return "an object table needs object_kind and an id column"
    if layout.row_kind == "requirement" and not {"description", "quantity"} <= set(roles):
        return "a requirement table needs a description and a quantity column"
    return None


def parse_layout(raw: object, chunk: Chunk, corpus: Corpus, cells: Cells) -> tuple[TableLayout | None, str | None]:
    try:
        layout = TableLayout.model_validate(raw)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) or "table" for e in exc.errors()})
        return None, f"doesn't match the table fields ({', '.join(fields)})"
    reason = check_layout(layout, chunk, corpus, cells)
    return (None, reason) if reason else (layout, None)


def read_table(
    layout: TableLayout, chunk: Chunk, corpus: Corpus, cells: Cells, skip: set[str]
) -> tuple[list[tuple[str, QuantityRecord]], dict[str, str]]:
    """(block id, record) for every row code read, and block id -> reason for every row it couldn't."""
    by_role = {c.role: c.col for c in layout.columns if c.role != "contains"}
    contains = [(c.col, c.object_kind) for c in layout.columns if c.role == "contains" and c.object_kind]
    read: list[tuple[str, QuantityRecord]] = []
    unread: dict[str, str] = {}
    for block_id in _rows(layout, chunk) or []:
        block = corpus.blocks[block_id]
        if block_id == layout.header_block_id or block_id in skip or block.kind not in _ROW_KINDS:
            continue
        row = cells.get(block_id)
        if row is None:
            unread[block_id] = "raden saknar celldata"
            continue
        cell = {c.col: c.text.strip() for c in row}

        def value(role: str, row_cells: dict[str, str] = cell) -> str:
            return row_cells.get(by_role.get(role, ""), "")

        cite = [Citation(block_id=block_id, quote=block.text)]
        common = {"type_code": value("type_code")[:40] or None, "location": value("location")[:120] or None}
        if layout.row_kind == "object":
            ident = value("id")
            if not ident:
                unread[block_id] = "id saknas i raden"
                continue
            held = []
            for col, kind in contains:
                n, _ = number_and_unit(cell.get(col, ""))
                if n is not None and n >= 0:
                    held.append(Contained(object_kind=kind, quantity=n))
            record = QuantityRecord(
                record_kind="object",
                what=f"{layout.object_kind} {ident}"[:200],
                object_kind=layout.object_kind,
                quantity_source="list_row",
                unit="st",
                scope=layout.scope,
                contains=held,
                citations=cite,
                **common,
            )
        else:
            what = value("description")
            quantity, trailing = number_and_unit(value("quantity"))
            if not what:
                unread[block_id] = "beskrivning saknas i raden"
                continue
            if quantity is None or quantity < 0:
                unread[block_id] = "mängdcellen är inte ett tal"
                continue
            unit = value("unit") or trailing or layout.unit or "st"
            record = QuantityRecord(
                record_kind="requirement",
                what=what[:200],
                quantity=quantity,
                quantity_source="stated",
                unit=unit[:20],
                scope=layout.scope,
                citations=cite,
                **common,
            )
        read.append((block_id, record))
    return read, unread


def cols_attr(block_id: str, cells: Cells) -> str | None:
    """The row's columns in cell order, for the prompt: `A,B,D`."""
    row = cells.get(block_id)
    return ",".join(c.col for c in row) if row else None
