"""A calc regenerated from typed posts, so a plant leaves nothing behind that gives it away.

Planted and control copies are both written here, from posts, in one layout. Totals, subtotals and numbering are
recomputed every time: removing posts leaves no stale total, no empty total and no gap in the numbering, and a section
left with no posts disappears.

Layout, per sheet (posts with no sheet go on "Kalkyl"):

    Nr | Benämning | Antal | Enhet | À-pris | Tim | Belopp
    1    <section>                                          heading
    1.1  <post>      ...                                    priced post
         <note>                                             a post with no quantity, price, hours or amount
         Summa <section>                 <hours>  <amount>  only for a section with priced posts
    ...
         Totalt                          <hours>  <amount>

- Amount is quantity × unit price, rounded to öre, when the post has both; otherwise the post's own amount.
- Posts keep their order inside a section; sections keep the order in which they first appear on their sheet. Posts
  with no section go in a closing section, "Övrigt".
- Cells hold values, never formulas: readers use the cached values, and openpyxl writes none for formulas.
- The file's bytes depend only on the posts: fixed document properties and fixed zip timestamps, so planting twice
  with the same seed gives the same file, the same calc post ids and the same cached requests.
"""

from __future__ import annotations

import datetime as dt
import io
import re
import zipfile
from dataclasses import dataclass, field

import openpyxl
from openpyxl.styles import Font

from anbud.granskning.contracts import CalcPost

HEADER = ("Nr", "Benämning", "Antal", "Enhet", "À-pris", "Tim", "Belopp")
DEFAULT_SHEET = "Kalkyl"
NO_SECTION = "Övrigt"
SUBTOTAL = "Summa "
TOTAL = "Totalt"
_FIXED_TIME = dt.datetime(2000, 1, 1)
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


@dataclass(frozen=True)
class PostPlace:
    """Where a post landed in a regenerated calc."""

    sheet: str
    row: int
    section: str
    number: str | None


def priced(post: CalcPost) -> bool:
    return any(v is not None for v in (post.quantity, post.unit_price, post.amount, post.hours))


def amount_of(post: CalcPost) -> float | None:
    if post.quantity is not None and post.unit_price is not None:
        return round(post.quantity * post.unit_price, 2)
    return post.amount


def _num(value: float | None) -> float | int | None:
    if value is None:
        return None
    return int(value) if float(value).is_integer() and abs(value) < 1e15 else value


@dataclass
class _Section:
    title: str
    posts: list[int] = field(default_factory=list)


def _layout(posts: list[CalcPost]) -> dict[str, list[_Section]]:
    sheets: dict[str, dict[str, _Section]] = {}
    for i, post in enumerate(posts):
        sheet = sheets.setdefault(post.sheet or DEFAULT_SHEET, {})
        title = post.section or NO_SECTION
        sheet.setdefault(title, _Section(title)).posts.append(i)
    out = {}
    for name, sections in sheets.items():
        ordered = [s for t, s in sections.items() if t != NO_SECTION]
        if NO_SECTION in sections:
            ordered.append(sections[NO_SECTION])
        out[name] = ordered
    return out


def regenerate(posts: list[CalcPost]) -> tuple[bytes, list[PostPlace]]:
    """The calc as xlsx bytes, and where each post landed (index for index with `posts`)."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    places: list[PostPlace | None] = [None] * len(posts)
    bold = Font(bold=True)
    for sheet_name, sections in _layout(posts).items():
        ws = wb.create_sheet(sheet_name)
        ws.append(list(HEADER))
        for cell in ws[1]:
            cell.font = bold
        total_hours = total_amount = 0.0
        k = 0
        for section in sections:
            k += 1
            ws.append([str(k), section.title])
            ws.cell(ws.max_row, 1).font = ws.cell(ws.max_row, 2).font = bold
            n = 0
            sub_hours = sub_amount = 0.0
            has_priced = False
            for i in section.posts:
                post = posts[i]
                if priced(post):
                    has_priced = True
                    n += 1
                    number = f"{k}.{n}"
                    amount = amount_of(post)
                    sub_hours += post.hours or 0.0
                    sub_amount += amount or 0.0
                    ws.append(
                        [
                            number,
                            post.text,
                            _num(post.quantity),
                            post.unit,
                            _num(post.unit_price),
                            _num(post.hours),
                            _num(amount),
                        ]
                    )
                else:
                    number = None
                    ws.append([None, post.text])
                if post.hidden:
                    ws.row_dimensions[ws.max_row].hidden = True
                places[i] = PostPlace(sheet_name, ws.max_row, section.title, number)
            if has_priced:
                sums = [_num(round(sub_hours, 2)), _num(round(sub_amount, 2))]
                ws.append([None, SUBTOTAL + section.title, None, None, None, *sums])
                total_hours += sub_hours
                total_amount += sub_amount
            ws.append([])
        ws.append([None, TOTAL, None, None, None, _num(round(total_hours, 2)), _num(round(total_amount, 2))])
        ws.cell(ws.max_row, 2).font = bold
        for col, width in zip("ABCDEFG", (8, 60, 10, 8, 12, 10, 14), strict=True):
            ws.column_dimensions[col].width = width
    wb.properties.creator = "Estim"
    wb.properties.created = wb.properties.modified = _FIXED_TIME
    buf = io.BytesIO()
    wb.save(buf)
    return _stable_zip(buf.getvalue()), [p for p in places if p is not None]


def _stable_zip(data: bytes) -> bytes:
    """The same archive with fixed member timestamps, so equal content gives equal bytes."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            member = zipfile.ZipInfo(info.filename, date_time=_ZIP_TIME)
            member.compress_type = zipfile.ZIP_DEFLATED
            member.external_attr = info.external_attr
            body = src.read(info.filename)
            if info.filename == "docProps/core.xml":
                body = re.sub(rb"<dcterms:modified[^>]*>[^<]*</dcterms:modified>", b"", body)
            dst.writestr(member, body)
    return out.getvalue()


@dataclass(frozen=True)
class Row:
    """One row of a calc in this module's layout, read back from the xlsx."""

    sheet: str
    row: int
    kind: str  # header, heading, post, note, subtotal, total
    number: str | None
    text: str
    quantity: float | None
    unit: str | None
    unit_price: float | None
    hours: float | None
    amount: float | None
    hidden: bool


_SECTION_NR = re.compile(r"^\d+$")
_POST_NR = re.compile(r"^\d+\.\d+$")


def _float(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def read_rows(data: bytes) -> list[Row]:
    """Every non-empty row of a regenerated calc, typed by what it is in the layout."""
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    rows: list[Row] = []
    try:
        for ws in wb.worksheets:
            for cells in ws.iter_rows(min_col=1, max_col=len(HEADER)):
                values = [c.value for c in cells]
                if all(v is None or str(v).strip() == "" for v in values):
                    continue
                r = cells[0].row
                nr = None if values[0] is None else str(values[0]).strip()
                text = "" if values[1] is None else str(values[1])
                if r == 1 and tuple(values) == HEADER:
                    kind = "header"
                elif nr and _POST_NR.match(nr):
                    kind = "post"
                elif nr and _SECTION_NR.match(nr):
                    kind = "heading"
                elif text == TOTAL:
                    kind = "total"
                elif text.startswith(SUBTOTAL):
                    kind = "subtotal"
                else:
                    kind = "note"
                hidden = bool(ws.row_dimensions[r].hidden) if r in ws.row_dimensions else False
                rows.append(
                    Row(
                        sheet=ws.title,
                        row=r,
                        kind=kind,
                        number=nr,
                        text=text,
                        quantity=_float(values[2]),
                        unit=None if values[3] is None else str(values[3]),
                        unit_price=_float(values[4]),
                        hours=_float(values[5]),
                        amount=_float(values[6]),
                        hidden=hidden,
                    )
                )
    finally:
        wb.close()
    return rows
