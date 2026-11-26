"""Calc ingest on synthetic xlsx files built in a temp dir."""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from anbud.granskning.calc import as_number, calc_files, header_role, load_calc, parse_calc


def _calc(path: Path) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Kalkyl"
    ws.append(["Exempelkalkyl"])
    ws.append([])
    ws.append(["Nr", "Benämning", "Antal", "Enhet", "Tim/st", "À-pris", "Summa"])
    ws.append([None, "Belysning"])
    ws.append(["1.1", "Trapphusarmatur", 35, "st", 0.5, 900, 31500])
    ws.append(["1.2", "Kabelstege", 0, "m", 0.2, 250, 0])
    ws.append(["1.3", "Dold post", 2, "st", 1, 100, 200])
    ws.row_dimensions[7].hidden = True
    ws.append([])
    ws.append(["Pos", "Moment", "Mängd", "Enh", "Timmar", "Belopp"])
    ws.append(["2.1", "Provisorisk el", "1 200", "m", "12,5", "4 500 kr"])
    summary = wb.create_sheet("Sammanställning")
    summary.append(["Sammanställning"])
    summary.append(["Del", "Material", "Arbete"])
    summary.append(["Belysning", 31700, 18])
    wb.save(path)
    return path


def test_headers_are_recognised_generically() -> None:
    assert header_role("Benämning") == "text"
    assert header_role("à-pris") == "unit_price"
    assert header_role("Summa kr") == "amount"
    assert header_role("Tim") == "hours"
    assert header_role("tim/st") == "hours_per_unit"
    assert header_role("Antal") == "quantity"
    assert header_role("Enhet") == "unit"
    assert header_role("Något helt annat här") is None
    assert as_number("1 234,50") == pytest.approx(1234.5)
    assert as_number("4 500 kr") == 4500
    assert as_number("två") is None


def test_posts_are_typed_with_stable_ids(tmp_path: Path) -> None:
    path = _calc(tmp_path / "calc.xlsx")
    posts = parse_calc(path)
    assert parse_calc(path) == posts
    by_text = {p.text: p for p in posts}
    lamp = by_text["Trapphusarmatur"]
    assert lamp.id.endswith("-s1-r5") and lamp.id.startswith("c") and len(lamp.id.split("-")[0]) == 9
    assert (lamp.quantity, lamp.unit, lamp.unit_price, lamp.amount) == (35, "st", 900, 31500)
    assert lamp.hours == pytest.approx(17.5)  # hours per unit times quantity
    assert lamp.section == "Belysning" and lamp.sheet == "Kalkyl" and lamp.row == 5
    assert by_text["Kabelstege"].quantity == 0 and by_text["Kabelstege"].amount == 0  # zero posts are kept
    assert by_text["Dold post"].hidden
    second = by_text["Provisorisk el"]  # a second table with its own header
    assert (second.quantity, second.unit, second.hours, second.amount) == (1200, "m", 12.5, 4500)
    assert "Belysning" not in by_text  # a section row is not a post
    untyped = [p for p in posts if p.sheet == "Sammanställning"]
    assert len(untyped) == 1 and untyped[0].id.endswith("-s2-r3")
    assert untyped[0].text == "Belysning: Material: 31700 | Arbete: 18" and untyped[0].amount is None


def test_subtotal_and_total_rows_are_not_posts(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Nr", "Benämning", "Antal", "Enhet", "À-pris", "Tim", "Belopp"])
    ws.append(["1", "Belysning"])
    ws.append(["1.1", "Trapphusarmatur", 35, "st", 900, 17.5, 31500])
    ws.append([None, "Summa Belysning", None, None, None, 17.5, 31500])
    ws.append(["2", "Kanalisation"])
    ws.append(["2.1", "Kabelstege", 120, "m", 250, 24, 30000])
    ws.append([None, "Delsumma", None, None, None, 24, 30000])
    ws.append([None, "Totalt", None, None, None, 41.5, 61500])
    ws.append([None, "Total:", None, None, None, None, 61500])
    ws.append(["3.1", "Totalt antal ljuspunkter enligt ritning", 40, "st", 100, 8, 4000])
    wb.save(tmp_path / "calc.xlsx")
    posts = parse_calc(tmp_path / "calc.xlsx")
    assert [p.text for p in posts] == ["Trapphusarmatur", "Kabelstege", "Totalt antal ljuspunkter enligt ritning"]
    assert [p.section for p in posts] == ["Belysning", "Kanalisation", "Kanalisation"]
    assert sum(p.amount or 0 for p in posts) == 65500


def test_calc_files_skip_lock_and_hidden_files(tmp_path: Path) -> None:
    calc_dir = tmp_path / "calc" / "export"
    calc_dir.mkdir(parents=True)
    _calc(calc_dir / "a.xlsx")
    (calc_dir / "~$a.xlsx").write_bytes(b"lock")
    (calc_dir / ".hidden.xlsx").write_bytes(b"x")
    assert [p.name for p in calc_files(tmp_path / "calc")] == ["a.xlsx"]
    assert len(load_calc(tmp_path / "calc")) == 5
    assert load_calc(tmp_path / "missing") == []
