"""The object list read by code, the way the Estim app reads it: apartments, premises, stairwells and stairwell floors.

Ported from the app's object model (`src/lib/tender-object-model.ts`, `buildObjectModel`, read-only), with its room
label rule and premises pattern from `src/lib/tender-object-counts.ts` (`roomCountFromLabel` is `rooms.room_count`,
`NON_APARTMENT` is here). Every sheet of the corpus is read on its own:

- **Type column.** The column with the most cells holding a room label ("3 rok", "2 rum och kök", "trea"); a sheet
  with fewer than five such cells is no object list.
- **Data rows.** Rows whose type cell holds a room label, and premises rows (a cell that starts or ends with "lokal").
  The header is every row above the first data row; anything below or between that is neither (a total, a note) is
  not read.
- **Premises.** A data row with a premises word (lokal, förråd, kontor, butik, garage, verksamhet, teknikrum,
  undercentral) in any cell and no room label, or a premises word in the type cell.
- **Columns by header.** The address column is the first other column whose header text matches
  `adress|gata|trapphus|uppgång|port`, the floor column `plan|våning`, the number column `lägenhetsnr|lgh nr|lgh`.
  The header text of a column is its cells in every header row, joined.
- **Counts.** Stairwells are the distinct non-empty addresses; stairwell floors the distinct address and floor pairs.
- **Buildings (Kalkyl's own, not the app's).** A column whose header names a building (`hus`, `huskropp`, `byggnad`,
  as a word of its own, so a stairwell column, "trapphus", is none; a building type is none either) groups the
  apartments by building: the distinct non-empty values are the buildings, a count labelled derived. The app only
  takes a building count from the text.

Several lists: the app keeps the sheet with the most apartments (the first of equals) and never adds lists together;
`choose` does the same, and the caller says how many lists it found.

The harness reads prep's cells (`prep/cells.jsonl`), not the app's text extraction. Both hold every cell, hidden or
not; a row prep split into a shown and a hidden block is joined again here. Areas and drawing type codes (the app's
kitchens, bathrooms and WC per apartment) are not read: Kalkyl takes those from the model's rows and the recipe.

The patterns are the app's generic notation of Swedish object lists, not words of any tender.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from anbud.granskning.corpus import Corpus
from anbud.kalkyl.rooms import room_count
from anbud.kalkyl.tables import Cells

APP_FILE = "src/lib/tender-object-model.ts"
MIN_TYPE_CELLS = 5
_LETTER = r"[^\W\d_]"
NON_APARTMENT = re.compile(
    rf"(?<!{_LETTER})(?:lokal|lokaler|förråd|kontor|butik|garage|verksamhet|teknikrum|undercentral)(?!{_LETTER})",
    re.IGNORECASE,
)
_LOKAL = re.compile(r"^lokal|lokal$", re.IGNORECASE)
# JS `\b` and `$` without flags: an ASCII word boundary, and the end of the header text.
_ASCII_B = r"(?<![A-Za-z0-9_])plan(?![A-Za-z0-9_])"
ADDRESS = re.compile(r"adress|gata|trapphus|uppgång|port", re.IGNORECASE)
FLOOR = re.compile(rf"^\s*plan\s*$|våning|{_ASCII_B}", re.IGNORECASE)
NUMBER = re.compile(r"lägenhetsnr|lgh\.?\s*nr|lägenhet\s*nr|lgh\Z", re.IGNORECASE)
BUILDING = re.compile(rf"(?<!{_LETTER})(?:huskropp|byggnad|hus)(?!s?\s*-?typ)", re.IGNORECASE)


@dataclass(frozen=True)
class ListApartment:
    block_id: str
    row: int
    rooms: float
    address: str | None
    floor: str | None
    number: str | None
    building: str | None = None


@dataclass(frozen=True)
class ObjectList:
    doc_id: str
    sheet: str
    type_column: str
    first_row: int
    last_row: int
    apartments: tuple[ListApartment, ...]
    premises_rows: tuple[str, ...]  # block ids
    address_column: str | None
    floor_column: str | None
    number_column: str | None
    building_column: str | None = None

    @property
    def premises(self) -> int:
        return len(self.premises_rows)

    def stairwells(self) -> dict[str, list[str]]:
        """Each distinct non-empty address, with the apartment rows that give it."""
        out: dict[str, list[str]] = {}
        for a in self.apartments:
            if a.address:
                out.setdefault(a.address, []).append(a.block_id)
        return out

    def buildings(self) -> dict[str, list[str]]:
        """Each distinct non-empty value of the building column, with the apartment rows that give it."""
        out: dict[str, list[str]] = {}
        for a in self.apartments:
            if a.building:
                out.setdefault(a.building, []).append(a.block_id)
        return out

    def stairwell_floors(self) -> dict[tuple[str, str], list[str]]:
        """Each distinct (address, floor) pair with both non-empty, with the apartment rows that give it."""
        out: dict[tuple[str, str], list[str]] = {}
        for a in self.apartments:
            if a.address and a.floor:
                out.setdefault((a.address, a.floor), []).append(a.block_id)
        return out

    @property
    def rows(self) -> list[str]:
        """Every row the list counts, apartments and premises, in sheet order."""
        return [a.block_id for a in self.apartments] + list(self.premises_rows)


def _col_index(col: str) -> int:
    n = 0
    for ch in col:
        n = n * 26 + ord(ch.upper()) - 64
    return n


Row = tuple[str, dict[str, str]]  # the row's first block id, column -> text


def sheet_rows(corpus: Corpus, cells: Cells) -> dict[tuple[str, str], dict[int, Row]]:
    """(doc id, sheet) -> row number -> row, every sheet row with cells, sheets and rows in reading order. A row's
    shown and hidden blocks are one row again, its cells in column order."""
    sheets: dict[tuple[str, str], dict[int, Row]] = {}
    for block_id in corpus.order:
        b = corpus.blocks[block_id]
        if b.kind != "sheet_row" or b.row is None or block_id not in cells:
            continue
        rows = sheets.setdefault((b.doc_id, b.sheet or ""), {})
        _, row = rows.setdefault(b.row, (block_id, {}))
        for c in cells[block_id]:
            row[c.col] = c.text.strip()
    for key, rows in sheets.items():
        ordered = sorted(rows.items())
        sheets[key] = {n: (bid, dict(sorted(r.items(), key=lambda kv: _col_index(kv[0])))) for n, (bid, r) in ordered}
    return sheets


def read_sheet(doc_id: str, sheet: str, rows: dict[int, Row]) -> ObjectList | None:
    """The app's object model for one sheet, or None when it has no type column."""
    columns = list(dict.fromkeys(c for _, r in rows.values() for c in r))

    def labelled(r: dict[str, str], col: str) -> bool:
        return room_count(r.get(col, ""), False) is not None

    hits = [(c, sum(labelled(r, c) for _, r in rows.values())) for c in columns]
    hits.sort(key=lambda x: -x[1])  # stable: the first column of equals
    if not hits or hits[0][1] < MIN_TYPE_CELLS:
        return None
    type_col = hits[0][0]
    data = [
        (n, bid, r)
        for n, (bid, r) in rows.items()
        if labelled(r, type_col) or any(NON_APARTMENT.search(v) and _LOKAL.search(v.strip()) for v in r.values())
    ]
    first = min(n for n, _, _ in data)

    def header(col: str) -> str:
        return " ".join(r.get(col, "") for n, (_, r) in rows.items() if n < first)

    def pick(pattern: re.Pattern[str]) -> str | None:
        return next((c for c in columns if c != type_col and pattern.search(header(c))), None)

    address, floor, number = pick(ADDRESS), pick(FLOOR), pick(NUMBER)
    building = next((c for c in columns if c not in (type_col, address) and BUILDING.search(header(c))), None)
    apartments: list[ListApartment] = []
    premises: list[str] = []
    for n, bid, r in data:
        label = r.get(type_col, "")
        rooms = room_count(label, False)
        if (any(NON_APARTMENT.search(v) for v in r.values()) and rooms is None) or NON_APARTMENT.search(label):
            premises.append(bid)
            continue
        if rooms is None:
            continue
        apartments.append(
            ListApartment(
                block_id=bid,
                row=n,
                rooms=rooms,
                address=r.get(address) if address else None,
                floor=r.get(floor) if floor else None,
                number=r.get(number) if number else None,
                building=r.get(building) if building else None,
            )
        )
    return ObjectList(
        doc_id=doc_id,
        sheet=sheet,
        type_column=type_col,
        first_row=first,
        last_row=max(n for n, _, _ in data),
        apartments=tuple(apartments),
        premises_rows=tuple(premises),
        address_column=address,
        floor_column=floor,
        number_column=number,
        building_column=building,
    )


def read_lists(corpus: Corpus, cells: Cells) -> list[ObjectList]:
    """Every sheet that is an object list by the app's rule, in reading order."""
    found = (read_sheet(doc, sheet, rows) for (doc, sheet), rows in sheet_rows(corpus, cells).items())
    return [lst for lst in found if lst is not None]


def choose(lists: list[ObjectList]) -> ObjectList | None:
    """The list the app uses: the most apartments, the first of equals. Lists are never added together."""
    best: ObjectList | None = None
    for lst in lists:
        if best is None or len(lst.apartments) > len(best.apartments):
            best = lst
    return best
