"""The cells sidecar: masked cells with their columns for every sheet_row and table_row block, and a sidecar-only
write that leaves an existing prep untouched. Synthetic files in temp folders."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_prep_sheets_docx import builder_for, workbook
from test_prep_tender import inbox, text_pdf

from anbud.prep import tender
from anbud.prep.build import DocBuilder, Piece
from anbud.prep.sheets import read_xlsx


def test_sheet_rows_keep_their_masked_cells_and_column_letters() -> None:
    builder = builder_for(data := workbook())
    read_xlsx(data, builder)
    cells = {c.block_id: c.cells for c in builder.cells}
    blocks = {b.id: b for b in builder.blocks}
    assert set(cells) == {b.id for b in builder.blocks if b.kind == "sheet_row"}
    for block_id, row in cells.items():
        assert " | ".join(c.text for c in row) == blocks[block_id].text  # the block text, cell by cell
    row2 = next(b for b in builder.blocks if (b.sheet, b.row, b.hidden) == ("Mängder", 2, False))
    assert [(c.col, c.text, c.numeric) for c in cells[row2.id]] == [
        ("A", "1", True),
        ("B", "Vägguttag", False),
        ("C", "24", True),
        ("D", "[namn 1]", False),  # masked like the block
    ]
    tucked = next(b for b in builder.blocks if (b.sheet, b.row, b.hidden) == ("Mängder", 2, True))
    assert [c.col for c in cells[tucked.id]] == ["E"]  # a hidden column keeps its own letter


def test_table_rows_get_positional_columns() -> None:
    builder = DocBuilder("ab" * 32, "abababababab")
    builder.add(Piece("table_row", "L1 | Downlight | 24"))
    builder.add(Piece("paragraph", "Ingen tabell 3 st"))
    (row,) = builder.cells
    assert [(c.col, c.text, c.numeric) for c in row.cells] == [
        ("1", "L1", False),
        ("2", "Downlight", False),
        ("3", "24", True),
    ]


def test_prepare_writes_the_sidecar(data_root: Path) -> None:
    folder = inbox(data_root, {"mangder.xlsx": workbook(), "a.pdf": text_pdf("Rubrik")})
    out = folder.parent / "prep"
    tender.prepare("co-001", folder, out, root=data_root)
    rows = [json.loads(line) for line in (out / "cells.jsonl").read_text().splitlines()]
    assert rows and all(r["cells"] for r in rows)


def test_the_sidecar_alone_leaves_the_prep_untouched_and_refuses_other_blocks(data_root: Path) -> None:
    folder = inbox(data_root, {"mangder.xlsx": workbook()})
    out = folder.parent / "prep"
    tender.prepare("co-001", folder, out, root=data_root)
    sidecar = (out / "cells.jsonl").read_bytes()
    (out / "cells.jsonl").unlink()  # a prep made before the sidecar existed
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    summary = tender.write_cells_sidecar("co-001", folder, out, root=data_root)
    assert summary["blocks_unchanged"] and summary["rows_with_cells"] > 0
    assert (out / "cells.jsonl").read_bytes() == sidecar
    assert {p.name: p.read_bytes() for p in out.iterdir() if p.name != "cells.jsonl"} == before
    (out / "cells.jsonl").unlink()
    (out / "blocks.jsonl").write_bytes(before["blocks.jsonl"] + b"\n")  # blocks from another prep
    with pytest.raises(tender.PrepError):
        tender.write_cells_sidecar("co-001", folder, out, root=data_root)
    assert not (out / "cells.jsonl").exists()
