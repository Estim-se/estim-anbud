"""Calc ingest: an xlsx calc export becomes typed `CalcPost`s.

- Every sheet is scanned top to bottom. A row whose cells read like Swedish calc headers (benämning, antal, enhet,
  à-pris, summa, tim and similar) starts a table; the columns it names are read from the rows below until the next
  header row. A sheet may hold several tables.
- A row with text and at least one number in a named column is a post. A row with text and no number is a section
  heading for the posts under it. Zero quantities and amounts are kept: a post priced at 0 is often the finding.
- Subtotal and total rows ("Summa ...", "Delsumma", "Totalt") are not posts: they would count their posts twice.
- Rows outside any table that hold text and numbers are kept as untyped posts: their values go into the text.
- Values are the calc's cached ones (openpyxl `data_only`); a formula without a cached value reads as empty.
- Post ids are stable: the file's sha256 prefix, the sheet's position and the row, for example `c1a2b3c4-s2-r17`.
  Sheet names never go into ids, so ids are safe in logs.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from anbud.granskning.contracts import CalcPost

ROLES = ("text", "quantity", "unit", "unit_price", "amount", "hours", "hours_per_unit", "code")
NUMERIC = frozenset({"quantity", "unit_price", "amount", "hours", "hours_per_unit"})

# Order matters: the first pattern that matches a header cell decides its role.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("hours_per_unit", re.compile(r"^(tim|tid|h)\w*\s*/\s*(st|enh|m)|^tid\s*per|^(tim|tid)\s*(à|á|a)\b|^enh\w*\s*tid")),
    ("unit_price", re.compile(r"^(à|á|a)[\s-]*pris|styckpris|enhetspris|pris\s*/|^pris\b|^à$|pris\s*per")),
    ("hours", re.compile(r"^tim|timmar|arbetstid|montagetid|^tid\b|^h$|^arb\w*\s*tid|^summa\s*tim|^tot\w*\s*tim")),
    ("amount", re.compile(r"summa|belopp|^total|totalt|kostnad|^sum$|^netto|^brutto|^kr$|^sek$")),
    ("quantity", re.compile(r"^antal|mängd|kvantitet|^ant\.?$|^st\.?$|^qty")),
    ("unit", re.compile(r"^enh(et)?\.?$|^enhet\b|^måttenhet")),
    ("text", re.compile(r"benämning|beskrivning|^text$|^artikel|^moment|specifikation|^post$|^rubrik|^arbete$")),
    ("code", re.compile(r"^nr\.?$|^pos|^kod|art\w*\s*nr|^id$|^e-?nr|^littera")),
]

_TOTAL_ROW = re.compile(
    r"^(?:(?:del|sub)?summa\b|s:a\b|summering\b|totalt?(?:\s*(?:summa|belopp|kr|sek|(?:exkl|inkl)\b.*))?\s*:?$)",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"^-?\d{1,3}(?:[\s ]\d{3})+(?:[.,]\d+)?$|^-?\d+(?:[.,]\d+)?$")


def header_role(value: Any) -> str | None:
    """The column role a header cell names, or None."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.casefold().replace(":", " ").split())
    if not text or len(text) > 40:
        return None
    for role, pattern in _PATTERNS:
        if pattern.search(text):
            return role
    return None


def as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        text = value.strip().removesuffix("kr").removesuffix(":-").strip()
        if _NUMBER.fullmatch(text):
            return float(text.replace(" ", "").replace(" ", "").replace(",", "."))
    return None


def _cell_text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = " ".join(value.split())
        return text or None
    return None


def _header_columns(cells: list[Any]) -> dict[str, int] | None:
    """Column index per role if the row is a header row: at least two roles, one of them numeric."""
    roles: dict[str, int] = {}
    amounts: list[tuple[int, str]] = []
    texts = 0
    for col, value in enumerate(cells):
        if _cell_text(value) is not None:
            texts += 1
        role = header_role(value)
        if role is None:
            continue
        if role == "amount":
            amounts.append((col, " ".join(str(value).casefold().split())))
        roles.setdefault(role, col)
    if amounts:  # prefer a plain total column, else the rightmost amount column
        plain = [c for c, t in amounts if re.fullmatch(r"(summa|totalt?|belopp)( kr| sek)?", t)]
        roles["amount"] = plain[0] if plain else amounts[-1][0]
    if len(roles) < 2 or not (NUMERIC & roles.keys()) or any(as_number(v) is not None for v in cells):
        return None
    if len(roles) * 2 < texts:  # mostly unrecognised words: a prose row, not a header
        return None
    return roles


def _text_column(rows: list[list[Any]], roles: dict[str, int]) -> int | None:
    """The text column of a table whose header didn't name one: the column left of the first numeric column that
    holds the most text in the rows below."""
    taken = set(roles.values())
    first_numeric = min(c for r, c in roles.items() if r in NUMERIC)
    counts: dict[int, int] = {}
    for cells in rows[:50]:
        for col, value in enumerate(cells[:first_numeric]):
            if col not in taken and _cell_text(value) is not None and as_number(value) is None:
                counts[col] = counts.get(col, 0) + 1
    return max(counts, key=lambda c: (counts[c], -c)) if counts else None


def _sheet_posts(prefix: str, index: int, ws: Any) -> list[CalcPost]:
    sheet_hidden = ws.sheet_state != "visible"
    hidden_rows = {r for r, d in ws.row_dimensions.items() if d.hidden}
    hidden_cols = {k for k, d in ws.column_dimensions.items() if d.hidden}
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    posts: list[CalcPost] = []
    roles: dict[str, int] | None = None
    text_col: int | None = None
    section: str | None = None
    labels: dict[int, str] = {}  # the last all-text row outside a table, read as column labels for untyped posts
    for i, cells in enumerate(rows):
        row = i + 1
        header = _header_columns(cells)
        if header is not None:
            roles, section = header, None
            text_col = roles.get("text")
            if text_col is None:
                text_col = _text_column(rows[i + 1 :], roles)
            continue
        if not any(v is not None and v != "" for v in cells):
            continue
        hidden = sheet_hidden or row in hidden_rows
        if roles is None:
            post = _untyped(prefix, index, ws.title, row, cells, section, hidden, labels)
            if post is not None:
                posts.append(post)
                continue
            texts = {c: t for c, v in enumerate(cells) if (t := _cell_text(v)) is not None}
            if len(texts) >= 2:
                labels = texts
            elif texts:
                section = next(iter(texts.values()))
            continue
        numbers = {r: as_number(cells[c]) if c < len(cells) else None for r, c in roles.items() if r in NUMERIC}
        text = _cell_text(cells[text_col]) if text_col is not None and text_col < len(cells) else None
        if text is None:
            text = _first_text(cells, skip=set(roles.values()))
        if text is None:
            continue
        if all(v is None for v in numbers.values()):
            section = text
            continue
        if _TOTAL_ROW.match(text):
            continue
        if text_col is not None:
            hidden = hidden or _column_letter(text_col) in hidden_cols
        hours = numbers.get("hours")
        if hours is None and numbers.get("hours_per_unit") is not None and numbers.get("quantity") is not None:
            hours = numbers["hours_per_unit"] * numbers["quantity"]
        unit_col = roles.get("unit")
        unit = _cell_text(cells[unit_col]) if unit_col is not None and unit_col < len(cells) else None
        posts.append(
            CalcPost(
                id=f"{prefix}-s{index}-r{row}",
                sheet=ws.title,
                row=row,
                section=section,
                text=text,
                quantity=numbers.get("quantity"),
                unit=unit,
                unit_price=numbers.get("unit_price"),
                amount=numbers.get("amount"),
                hours=hours,
                hidden=hidden,
            )
        )
    return posts


def _first_text(cells: list[Any], skip: set[int] | None = None) -> str | None:
    for col, value in enumerate(cells):
        if skip and col in skip:
            continue
        text = _cell_text(value)
        if text is not None and as_number(value) is None:
            return text
    return None


def _untyped(
    prefix: str,
    index: int,
    sheet: str,
    row: int,
    cells: list[Any],
    section: str | None,
    hidden: bool,
    labels: dict[int, str],
) -> CalcPost | None:
    label = _first_text(cells)
    if label is None or not any(as_number(v) is not None for v in cells):
        return None
    rest = []
    seen_label = False
    for col, value in enumerate(cells):
        if value is None or value == "":
            continue
        number = as_number(value)
        shown = _fmt(number) if number is not None else " ".join(str(value).split())
        if not seen_label and shown == label:
            seen_label = True
            continue
        rest.append(f"{labels[col]}: {shown}" if col in labels and labels[col] != shown else shown)
    return CalcPost(
        id=f"{prefix}-s{index}-r{row}",
        sheet=sheet,
        row=row,
        section=section,
        text=f"{label}: {' | '.join(rest)}",
        hidden=hidden,
    )


def _fmt(value: float | None) -> str:
    if value is None:
        return ""
    return str(int(value)) if value == int(value) else f"{value:.2f}"


def _column_letter(col: int) -> str:
    from openpyxl.utils import get_column_letter

    return get_column_letter(col + 1)


def parse_calc(path: Path) -> list[CalcPost]:
    """Every post in one xlsx calc, in sheet and row order."""
    import openpyxl

    data = path.read_bytes()
    prefix = "c" + hashlib.sha256(data).hexdigest()[:8]
    wb = openpyxl.load_workbook(path, data_only=True)
    try:
        posts: list[CalcPost] = []
        for index, ws in enumerate(wb.worksheets, start=1):
            posts.extend(_sheet_posts(prefix, index, ws))
        return posts
    finally:
        wb.close()


def calc_files(calc_dir: Path) -> list[Path]:
    """The xlsx files under a tender's calc folder, in a stable order. Office lock files and hidden files are left
    out."""
    if not calc_dir.is_dir():
        return []
    return sorted(
        p
        for p in calc_dir.rglob("*.xlsx")
        if p.is_file() and not any(part.startswith((".", "~$")) for part in p.relative_to(calc_dir).parts)
    )


def load_calc(calc_dir: Path) -> list[CalcPost]:
    posts: list[CalcPost] = []
    for path in calc_files(calc_dir):
        posts.extend(parse_calc(path))
    return posts
