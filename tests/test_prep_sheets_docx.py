from __future__ import annotations

import datetime as dt
import hashlib
import io

import openpyxl
from synthetic_docs import docx_paragraph, docx_table, make_docx, personnummer

from anbud.prep.build import DocBuilder
from anbud.prep.docx import read_docx
from anbud.prep.sheets import read_xlsx


def builder_for(data: bytes) -> DocBuilder:
    sha = hashlib.sha256(data).hexdigest()
    return DocBuilder(sha, sha[:12])


def workbook() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Mängder"
    ws.append(["Pos", "Benämning", "Antal", "Namn", "Notering"])
    ws.append([1, "Vägguttag", 24, "Kim Exempel", personnummer("811218")])
    ws.append([2, "Belysning", 12.0, "Kim Exempel", "dold rad"])
    ws.append([3, "Datum", dt.datetime(2026, 3, 1), "Ny Person", 0.5])
    ws.append([int(personnummer("811218").replace("-", "")), "Numeriskt id", 1, None, None])
    ws.row_dimensions[3].hidden = True
    ws.column_dimensions["E"].hidden = True
    ws.merge_cells("A7:C7")
    ws["A7"] = "Sammanslagen rubrik"
    secret = wb.create_sheet("Intern")
    secret.append(["Påslag", 0.12])
    secret.sheet_state = "hidden"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_rows_hidden_state_and_values() -> None:
    builder = builder_for(data := workbook())
    infos = read_xlsx(data, builder)
    rows = {(b.sheet, b.row, b.hidden): b.text for b in builder.blocks if (b.sheet, b.row) != ("Mängder", 3)}
    hidden_row = [(b.text, b.hidden) for b in builder.blocks if (b.sheet, b.row) == ("Mängder", 3)]
    assert hidden_row == [("2 | Belysning | 12 | [namn 1]", True), ("dold rad", True)]  # 12.0 shown as 12
    assert rows[("Mängder", 1, False)] == "Pos | Benämning | Antal | Namn"
    assert rows[("Mängder", 1, True)] == "Notering"  # a hidden column's cells: their own hidden block
    assert rows[("Mängder", 2, False)] == "1 | Vägguttag | 24 | [namn 1]"
    assert rows[("Mängder", 2, True)] == "[personnummer]"
    assert rows[("Mängder", 4, False)] == "3 | Datum | 2026-03-01 | [namn 2]"
    assert rows[("Mängder", 7, False)] == "Sammanslagen rubrik"
    assert rows[("Intern", 1, True)] == "Påslag | 0.12"  # every row of a hidden sheet is hidden
    by_name = {i.name: i for i in infos}
    assert by_name["Mängder"].hidden_rows == 1 and by_name["Mängder"].hidden_cols == 1
    assert by_name["Mängder"].merged == 1 and by_name["Intern"].state == "hidden"
    assert [o.title for o in builder.outline] == ["Mängder", "Intern"]


def test_numeric_cells_are_never_masked() -> None:
    builder = builder_for(data := workbook())
    read_xlsx(data, builder)
    numeric = next(b for b in builder.blocks if b.sheet == "Mängder" and b.row == 5)
    assert numeric.text.startswith(personnummer("811218").replace("-", ""))
    kinds = sorted(m.kind for m in builder.masked)
    assert kinds == ["name", "name", "name", "personnummer"]


def test_docx_headings_tables_and_tracked_changes() -> None:
    nested = "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>inre</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    body = (
        docx_paragraph("Allmänt", style="Rubrik1")
        + docx_paragraph("2.1 Omfattning", style="Heading2")
        + docx_paragraph("Texten gäller", deleted="borttagen text")
        + docx_table([["<w:p><w:r><w:t>Post</w:t></w:r></w:p>", "<w:p><w:r><w:t>Antal</w:t></w:r></w:p>" + nested]])
    )
    builder = builder_for(data := make_docx(body))
    read_docx(data, builder)
    kinds = [(b.kind, b.text) for b in builder.blocks]
    assert kinds == [
        ("heading", "Allmänt"),
        ("heading", "2.1 Omfattning"),
        ("paragraph", "Texten gäller"),
        ("table_row", "Post | Antal inre"),
    ]
    assert [(o.level, o.number) for o in builder.outline] == [(1, None), (2, "2.1")]
