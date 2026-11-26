"""Reports: escaped HTML, a Swedish XLSX that review columns can extend, written outside the repo only."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from _granskning_synth import CALC_POSTS, basis, catalog, corpus, flag, lens_result
from openpyxl import load_workbook

from anbud.granskning.merge import items_from_results
from anbud.granskning.rank import coverage, price_items, rank
from anbud.granskning.report import (
    FLAG_COLUMNS,
    Column,
    build_report,
    build_workbook,
    column_keys,
    render_html,
    report_dir,
    write_reports,
)
from anbud.granskning.settings import load_settings
from anbud.paths import repo_root

S = load_settings()
HOSTILE_QUOTE = "Byggström tillhandahålls av beställaren"
HOSTILE_CLAIM = '<script>alert(1)</script> **fet** [länk](http://x) =HYPERLINK("http://x") 120 timmar'


def _report():
    flags = [
        flag("contract", HOSTILE_CLAIM, money_basis=basis(kind="hours_gap", hours=120)),
        flag("contract", "Mindre sak.", claim_type="t2", money_basis=basis(kind="hours_gap", hours=1)),
        flag("generic_scope", "Normal omfattning.", claim_type="t3", money_basis=basis(item="Vägguttag", quantity=80)),
    ]
    results = [lens_result("contract", flags[:2]), lens_result("generic_scope", flags[2:], generic=True)]
    items = price_items(items_from_results(results), catalog(), {p.id: p for p in CALC_POSTS}, S)
    return build_report(
        run_id="run-0001",
        company_id="co-001",
        tender_id="t-0001",
        ranked=rank(items, S),
        coverage=coverage(results, corpus()),
        corpus=corpus(),
        catalog=catalog(),
        materiality_sek=S.materiality_sek,
        created_at=datetime(2026, 11, 26, 12, 0, tzinfo=UTC),
    )


def test_html_escapes_every_model_and_document_string() -> None:
    page = render_html(_report())
    assert "<script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "**fet**" in page and "<strong>fet</strong>" not in page  # never rendered as markdown
    assert "[länk](http://x)" in page and '<a href="http://x"' not in page
    assert HOSTILE_QUOTE in page and "Beskrivning.pdf, s. 5" in page
    assert "default-src 'none'" in page
    assert "Estims standardvärden" in page and "Generiska fynd (1)" in page
    assert "Täckningen visar bara" in page and "Mindre (under väsentlighetsgränsen)" in page


def test_the_workbook_has_swedish_labels_text_cells_and_keys() -> None:
    reviewer = (Column("decision", "Beslut", 12), Column("minutes", "Minuter", 8))
    wb = build_workbook(_report(), extra_columns=reviewer)
    assert wb.sheetnames == ["Flaggor", "Generiska", "Loggade", "Täckning", "Om rapporten", "_nycklar"]
    assert wb["_nycklar"].sheet_state == "hidden"
    ws = wb["Flaggor"]
    labels = [c.value for c in ws[1]]
    assert labels[:4] == ["Fingeravtryck", "Lins", "Typ", "Påstående"] and labels[-2:] == ["Beslut", "Minuter"]
    keys = column_keys(wb, "Flaggor")
    assert keys["claim"] == 4 and keys["decision"] == len(FLAG_COLUMNS) + 1
    claim = ws.cell(row=2, column=keys["claim"])
    assert claim.data_type == "s" and claim.value.startswith("<script>")  # stored as text, never a formula
    assert ws.cell(row=2, column=keys["decision"]).value is None
    assert ws.cell(row=2, column=keys["kronor_high"]).value == round(120 * 500 * 1.3)
    assert ws.cell(row=2, column=keys["default_catalog"]).value == "Ja"
    assert wb["Generiska"].max_row == 2 and wb["Loggade"].max_row == 2


def test_formula_text_stays_text_after_saving(tmp_path: Path) -> None:
    path = tmp_path / "r.xlsx"
    build_workbook(_report()).save(path)
    ws = load_workbook(path)["Flaggor"]
    cells = [c for row in ws.iter_rows(min_row=2) for c in row if isinstance(c.value, str)]
    assert any("=HYPERLINK" in c.value for c in cells)
    assert all(c.data_type == "s" for c in cells)


def test_reports_go_to_the_tender_folder_never_the_repo(data_root: Path) -> None:
    out = report_dir("co-001", "t-0001", "run-0001")
    assert out == data_root / "co-001" / "dev" / "t-0001" / "reports" / "run-0001"
    paths = write_reports(_report(), out)
    assert paths["html"].read_text(encoding="utf-8").startswith("<!doctype html>")
    assert load_workbook(paths["xlsx"]).sheetnames[0] == "Flaggor"
    with pytest.raises(ValueError):
        write_reports(_report(), repo_root() / "out")
    with pytest.raises(ValueError):
        report_dir("co-001", "t-0001", "../x")
