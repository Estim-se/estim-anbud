"""Control arm 1: a code-only structural detector over a regenerated calc. No model, no tender documents.

It catches what a plant would give away to anyone scanning the sheet: numbering gaps, amounts that aren't quantity ×
unit price, subtotals and totals that don't add up or are empty, headings with nothing under them, and catalog diffs.
A plant it catches is too easy, so planted recall leaves it out.

Catalog diffs come in two forms:
- against a catalog, when one is given: unit price or hours per unit more than `TOLERANCE` off the catalog's;
- against the calc's own norm: the same item (text and unit) priced or timed differently elsewhere in the calc.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from anbud.evals.planted.calc import Row, read_rows
from anbud.prep.text import normalise

TOLERANCE = 0.2
MONEY_SLACK = 0.5


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    check: str
    sheet: str
    row: int
    section: str | None = None


@dataclass(frozen=True)
class CatalogItem:
    unit_price: float | None = None
    hours_per_unit: float | None = None


def item_key(text: str, unit: str | None) -> str:
    return normalise(text).casefold() + "|" + normalise(unit or "").casefold()


def _off(value: float, norm: float) -> bool:
    return norm > 0 and abs(value - norm) / norm > TOLERANCE


def detect(data: bytes, catalog: dict[str, CatalogItem] | None = None) -> list[Finding]:
    """Every structural finding in a regenerated calc, in sheet and row order."""
    rows = read_rows(data)
    findings: list[Finding] = []
    by_sheet: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        by_sheet[row.sheet].append(row)
    posts: list[tuple[Row, str | None]] = []
    for sheet, sheet_rows in by_sheet.items():
        findings += _sheet(sheet, sheet_rows, posts)
    findings += _norms(posts, catalog)
    return sorted(set(findings), key=lambda f: (f.sheet, f.row, f.check))


def _sheet(sheet: str, rows: list[Row], posts: list[tuple[Row, str | None]]) -> list[Finding]:
    out: list[Finding] = []
    section: str | None = None
    heading_row = 0
    expected_section = 1
    expected_post = 1
    content = 0
    sub_hours = sub_amount = 0.0
    priced = 0
    subtotals: list[tuple[float, float]] = []

    def close(at: int) -> None:
        if section is not None and content == 0:
            out.append(Finding(check="empty_section", sheet=sheet, row=heading_row, section=section))
        if section is not None and priced and not closed:
            out.append(Finding(check="missing_subtotal", sheet=sheet, row=at, section=section))

    closed = True
    for row in rows:
        if row.kind == "heading":
            close(row.row)
            if row.number != str(expected_section):
                out.append(Finding(check="section_numbering", sheet=sheet, row=row.row, section=row.text))
            expected_section = int(row.number or 0) + 1
            section, heading_row, content, priced, closed = row.text, row.row, 0, 0, False
            expected_post = 1
            sub_hours = sub_amount = 0.0
        elif row.kind == "post":
            content += 1
            priced += 1
            major, minor = (row.number or "0.0").split(".")
            if section is None or major != str(expected_section - 1) or minor != str(expected_post):
                out.append(Finding(check="post_numbering", sheet=sheet, row=row.row, section=section))
            expected_post = int(minor) + 1
            if row.quantity is not None and row.unit_price is not None:
                if row.amount is None or abs(row.quantity * row.unit_price - row.amount) > MONEY_SLACK:
                    out.append(Finding(check="amount", sheet=sheet, row=row.row, section=section))
            sub_hours += row.hours or 0.0
            sub_amount += row.amount or 0.0
            posts.append((row, section))
        elif row.kind == "note":
            content += 1
        elif row.kind == "subtotal":
            closed = True
            if row.amount is None and row.hours is None:
                out.append(Finding(check="empty_subtotal", sheet=sheet, row=row.row, section=section))
            elif not priced:
                out.append(Finding(check="subtotal_without_posts", sheet=sheet, row=row.row, section=section))
            if abs((row.amount or 0.0) - sub_amount) > MONEY_SLACK or abs((row.hours or 0.0) - sub_hours) > 0.05:
                out.append(Finding(check="subtotal", sheet=sheet, row=row.row, section=section))
            subtotals.append((row.hours or 0.0, row.amount or 0.0))
        elif row.kind == "total":
            close(row.row)
            section = None
            hours = sum(h for h, _ in subtotals)
            amount = sum(a for _, a in subtotals)
            if row.amount is None:
                out.append(Finding(check="empty_total", sheet=sheet, row=row.row))
            elif abs(row.amount - amount) > MONEY_SLACK or abs((row.hours or 0.0) - hours) > 0.05:
                out.append(Finding(check="total", sheet=sheet, row=row.row))
    if section is not None:
        close(rows[-1].row)
    if not any(r.kind == "total" for r in rows):
        out.append(Finding(check="missing_total", sheet=sheet, row=rows[-1].row if rows else 1))
    return out


def _norms(posts: list[tuple[Row, str | None]], catalog: dict[str, CatalogItem] | None) -> list[Finding]:
    out: list[Finding] = []
    groups: dict[str, list[tuple[Row, str | None]]] = defaultdict(list)
    for row, section in posts:
        groups[item_key(row.text, row.unit)].append((row, section))
    for key, members in groups.items():
        for attr, check in (("unit_price", "price_norm"), ("hours_per_unit", "hours_norm")):
            values = [(m, _value(m[0], attr)) for m in members]
            values = [(m, v) for m, v in values if v is not None]
            if len(values) >= 2:
                norm = statistics.median(v for _, v in values)
                out += [_at(check, m) for m, v in values if _off(v, norm)]
        item = (catalog or {}).get(key)
        if item is None:
            continue
        for member in members:
            price = _value(member[0], "unit_price")
            if item.unit_price is not None and price is not None and _off(price, item.unit_price):
                out.append(_at("catalog_price", member))
            per_unit = _value(member[0], "hours_per_unit")
            if item.hours_per_unit is not None and per_unit is not None and _off(per_unit, item.hours_per_unit):
                out.append(_at("catalog_hours", member))
    return out


def _value(row: Row, attr: str) -> float | None:
    if attr == "unit_price":
        return row.unit_price
    if row.hours is None or not row.quantity:
        return None
    return row.hours / row.quantity


def _at(check: str, member: tuple[Row, str | None]) -> Finding:
    row, section = member
    return Finding(check=check, sheet=row.sheet, row=row.row, section=section)
