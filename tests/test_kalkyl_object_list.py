"""The object list read by code as the app reads it: type column, header, premises, stairwells, and how its counts
meet the model's. Every sheet, text, address and count here is invented; no model call is real, $0."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import openpyxl
import pytest
from kalkyl_synthetic import (
    BLOCKS,
    DOC_A,
    GOOD,
    OUTLINE,
    cite,
    fake_sdk,
    first_block,
    good_handler,
    message,
    obj,
    req,
    submit,
    write_catalog,
    write_cells,
    write_prep,
)

from anbud import paths
from anbud.granskning.corpus import Corpus
from anbud.kalkyl.object_list import choose, read_lists
from anbud.kalkyl.price import ASSUMED_COUNT_NOTE, CODE_BASIS, count_objects
from anbud.kalkyl.records import QuantityRecord, VerifiedRecord
from anbud.kalkyl.rooms import room_count
from anbud.kalkyl.run import run_dev_tender
from anbud.kalkyl.tables import load_cells
from anbud.models import Block

DOC = "cccccccccccc"
OTHER = "dddddddddddd"

# A title row, a header, ten apartments in two stairwells on three floors, three premises, then a tail.
LIST = [
    {"A": "Objektförteckning etapp 1"},
    {"A": "Lgh nr", "B": "Typ", "C": "BOA", "D": "Trapphus", "E": "Plan"},
    {"A": "1001", "B": "1 rok", "C": "35", "D": "Exempelgatan 1", "E": "1"},
    {"A": "1002", "B": "2 rok", "C": "52", "D": "Exempelgatan 1", "E": "1"},
    {"A": "1003", "B": "3 rum och kök", "C": "71", "D": "Exempelgatan 1", "E": "2"},
    {"A": "1004", "B": "3,5 rok", "C": "80", "D": "Exempelgatan 1", "E": "2"},
    {"A": "1005", "B": "trea", "C": "70", "D": "Exempelgatan 1", "E": "3"},
    {"A": "2001", "B": "2 rok", "C": "50", "D": "Exempelgatan 3", "E": "1"},
    {"A": "2002", "B": "4 rok", "C": "95", "D": "Exempelgatan 3", "E": "1"},
    {"A": "2003", "B": "Typ B 2 r.o.k", "C": "55", "D": "Exempelgatan 3", "E": "2"},
    {"A": "2004", "B": "3½ rum", "C": "78", "D": "Exempelgatan 3", "E": "2"},
    {"A": "2005", "B": "1 rok", "C": "33", "D": "Exempelgatan 3", "E": "3"},
    {"A": "L01", "B": "Lokal", "C": "120", "D": "Exempelgatan 1", "E": "0"},
    {"A": "L02", "C": "80", "F": "Lokal"},  # a premises row recognised outside the type column
    {"A": "K01", "B": "Kontor 2 rum", "C": "60", "D": "Exempelgatan 3", "E": "0"},  # rooms, but premises
    {},
    {"A": "Summa", "C": "879"},
    {"A": "Förråd", "C": "5"},  # a premises word, but not a list row: no room label and not "lokal"
    {"A": "Antal lägenheter", "B": "10"},
]


def _sheet(doc: str, sheet: str, rows: list[dict[str, str]], start: int = 1) -> tuple[list[Block], dict]:
    blocks, cells = [], {}
    for n, row in enumerate(rows, start):
        if not row:
            continue
        b = Block(
            id=f"{doc[:6]}-{n:06d}", doc_id=doc, kind="sheet_row", text=" | ".join(row.values()), sheet=sheet, row=n
        )
        blocks.append(b)
        cells[b.id] = [{"col": c, "text": t, "numeric": t.isdigit()} for c, t in row.items()]
    return blocks, cells


def _corpus(tmp_path: Path, *sheets: tuple[list[Block], dict]) -> tuple[Corpus, dict]:
    blocks = [b for s in sheets for b in s[0]]
    prep = write_prep(tmp_path / "prep", blocks, [])
    write_cells(prep, {k: v for s in sheets for k, v in s[1].items()})
    return Corpus.load(prep), load_cells(prep / "cells.jsonl")


def _verified(records: list[dict]) -> list[VerifiedRecord]:
    out = []
    for n, raw in enumerate(records, 1):
        r = QuantityRecord.model_validate(raw)
        ids = [c.block_id for c in r.citations]
        out.append(VerifiedRecord(id=f"c001-r{n:03d}", chunk_id="c001", record=r, level="exact", block_ids=ids))
    return out


@pytest.mark.parametrize(
    ("label", "bare", "expected"),
    [
        ("3 rok", False, 3),
        ("3 RoK", False, 3),
        ("4 rum och kök", False, 4),
        ("Typ B 2 r.o.k", False, 2),
        ("2 r + k", False, 2),
        ("trea", False, 3),
        ("tre rum", False, 3),
        ("3,5 rok", False, 3.5),
        ("3½ rum", False, 3.5),
        ("3", False, None),  # a bare number only from a room-count column
        ("3", True, 3),
        ("120 kvm", False, None),
        ("0 rum", False, None),
        ("Rumsnummer 12", False, None),
        ("", False, None),
    ],
)
def test_room_count_reads_one_cell_as_the_app_does(label: str, bare: bool, expected: float | None) -> None:
    assert room_count(label, bare) == expected


def test_a_sheet_is_read_as_the_app_reads_its_object_list(tmp_path: Path) -> None:
    corpus, cells = _corpus(tmp_path, _sheet(DOC, "Objekt", LIST))
    (lst,) = read_lists(corpus, cells)
    assert (lst.type_column, lst.first_row, lst.last_row) == ("B", 3, 15)  # title and header above, tail below
    assert (lst.address_column, lst.floor_column, lst.number_column) == ("D", "E", "A")
    assert len(lst.apartments) == 10 and lst.premises == 3
    assert [a.rooms for a in lst.apartments] == [1, 2, 3, 3.5, 3, 2, 4, 2, 3.5, 1]
    assert lst.apartments[0].block_id == "cccccc-000003" and lst.apartments[0].number == "1001"
    assert lst.premises_rows == ("cccccc-000013", "cccccc-000014", "cccccc-000015")
    assert len(lst.stairwells()) == 2  # premises rows' addresses are not stairwells
    assert len(lst.stairwell_floors()) == 6  # two stairwells on three floors
    assert lst.stairwells()["Exempelgatan 3"] == [f"cccccc-{n:06d}" for n in range(8, 13)]


def test_a_sheet_with_fewer_than_five_room_labels_is_no_list(tmp_path: Path) -> None:
    corpus, cells = _corpus(tmp_path, _sheet(DOC, "Objekt", LIST[:6]))  # four labelled rows
    assert read_lists(corpus, cells) == []


def test_without_address_or_floor_headers_there_is_no_stairwell_count(tmp_path: Path) -> None:
    rows = [dict(r) for r in LIST]
    rows[1] = {**rows[1], "D": "Kolumn D", "E": "Anm"}
    corpus, cells = _corpus(tmp_path, _sheet(DOC, "Objekt", rows))
    (lst,) = read_lists(corpus, cells)
    assert lst.address_column is None and lst.floor_column is None
    assert lst.stairwells() == {} and lst.stairwell_floors() == {}
    objects = count_objects([], None, [lst])
    assert "stairwell" not in objects and "stairwell_floor" not in objects
    assert objects["apartment"].count == 10


def test_a_row_split_into_shown_and_hidden_blocks_is_one_row(tmp_path: Path) -> None:
    blocks, cells = _sheet(DOC, "Objekt", LIST)
    # Prep writes hidden columns as a second block of the same row: move every address to its own block.
    extra, extra_cells = [], {}
    for b in list(blocks):
        address = [c for c in cells[b.id] if c["col"] == "D"]
        if not address:
            continue
        cells[b.id] = [c for c in cells[b.id] if c["col"] != "D"]
        hidden = Block(id=f"{b.id}-h", doc_id=DOC, kind="sheet_row", text="x", sheet="Objekt", row=b.row, hidden=True)
        extra.append(hidden)
        extra_cells[hidden.id] = address
    corpus, loaded = _corpus(tmp_path, (blocks + extra, {**cells, **extra_cells}))
    (lst,) = read_lists(corpus, loaded)
    assert lst.address_column == "D" and len(lst.stairwells()) == 2
    assert lst.apartments[0].block_id == "cccccc-000003"  # cited by the row's first block


def test_two_lists_are_never_added_the_larger_is_used_as_in_the_app(tmp_path: Path) -> None:
    small = LIST[:2] + LIST[2:8]  # six apartments
    corpus, cells = _corpus(tmp_path, _sheet(OTHER, "Etapp 2", small), _sheet(DOC, "Objekt", LIST))
    lists = read_lists(corpus, cells)
    assert [len(x.apartments) for x in lists] == [6, 10]
    assert choose(lists) is lists[1]
    objects = count_objects([], None, lists)
    assert objects["apartment"].count == 10
    assert "2 listor hittades" in objects["apartment"].basis and "adderas inte" in objects["apartment"].basis
    assert choose([lists[1], lists[1]]) is lists[1]  # equals: the first


def test_the_code_list_counts_objects_and_rooms_and_keeps_the_models_evidence(tmp_path: Path) -> None:
    corpus, cells = _corpus(tmp_path, _sheet(DOC, "Objekt", LIST))
    lists = read_lists(corpus, cells)
    tail = [n for n in range(3, 19) if n != 16]  # every apartment, the premises and two tail rows
    model = [obj("apartment", "list_row", [cite(f"cccccc-{n:06d}", "x")]) for n in tail]
    model += [obj("apartment", "stated", [cite("t-1", "12")], quantity=12)]
    model += [obj("premises", "stated", [cite("t-2", "3")], quantity=3)]
    model += [obj("building", "stated", [cite("t-3", "2")], quantity=2)]
    objects = count_objects(_verified(model), None, lists)
    apartments = objects["apartment"]
    assert apartments.count == 10 and apartments.source == "code"
    assert CODE_BASIS in apartments.basis and "modellen: 15 (15 rader i lista); 12 (angivet totalt antal)" in (
        apartments.basis
    )
    assert apartments.conflict == "Konflikt: 10 enligt objektlistan / 12 / 15 enligt modellen – objektlistan används"
    assert apartments.rows == [a.block_id for a in lists[0].apartments]
    assert len(apartments.record_ids) == 16  # the model's records stay with the count
    # The model's sources outside the list: the premises and tail rows it counted, and its stated total.
    assert apartments.model_rows == [f"cccccc-{n:06d}" for n in (13, 14, 15, 17, 18)] + ["t-1"]
    assert objects["premises"].count == 3 and objects["premises"].conflict is None  # list and model agree
    assert (objects["stairwell"].count, objects["stairwell_floor"].count) == (2, 6)
    assert objects["building"].source == "model" and objects["building"].count == 2
    # Room types from the type column: 1+2+3+3.5+3+2+4+2+3.5+1 rooms; one living room each; whole rooms - 1 bedrooms.
    assert objects["room"].count == 25 and objects["living_room"].count == 10
    assert objects["bedroom"].count == 14 and objects["half_room"].count == 2
    assert "per lägenhetstyp: 1 rum: 2 lgh × 0" not in objects["bedroom"].basis
    assert "3,5 rum: 2 lgh × 2" in objects["bedroom"].basis


def test_without_a_list_the_model_counts_as_before() -> None:
    records = _verified(
        [
            obj("apartment", "list_row", [cite("r-1", "x")]),
            obj("apartment", "list_row", [cite("r-2", "x")]),
            obj("apartment", "stated", [cite("t", "3")], quantity=3),
        ]
    )
    for lists in (None, []):
        apartments = count_objects(records, None, lists)["apartment"]
        assert apartments.count == 2 and apartments.source == "model"
        assert apartments.conflict == "Konflikt: 2 / 3 – lägsta används"


@pytest.fixture
def tender(data_root: Path) -> Path:
    tender = paths.dev_tender_dir("co-001", "t-0001")
    blocks, cells = _sheet(DOC, "Objekt", LIST)
    prep = write_prep(tender / "prep", [*BLOCKS, *blocks])
    write_cells(prep, cells)
    write_catalog(data_root)
    return tender


def test_a_run_counts_from_the_code_list_and_says_where_the_model_disagrees(tender: Path) -> None:
    summary = run_dev_tender("co-001", "t-0001", sdk=fake_sdk(), catalog_name="synthetic", run_id="kalkyl-test")
    assert summary["object_lists_found"] == 1
    assert summary["objects_from_code"] == {
        "apartment": 10,
        "bedroom": 14,
        "half_room": 2,
        "living_room": 10,
        "premises": 3,
        "room": 25,
        "stairwell": 2,
        "stairwell_floor": 6,
    }
    assert summary["object_counts"]["apartment"] == 10
    # The model counted the three rows of another sheet: they disagree, and the rows stay with the count.
    assert summary["objects_code_model_disagree"] == ["apartment"]
    assert summary["list_rows_counted_by_code"] == 13
    out = tender / "kalkyl" / "kalkyl-test"
    result = json.loads((out / "result.json").read_text())
    apartments = next(o for o in result["objects"] if o["kind"] == "apartment")
    assert apartments["model_rows"] == ["bbbbbb-000002", "bbbbbb-000003", "bbbbbb-000004"]
    posts = json.loads((out / "posts.json").read_text())
    assert next(p for p in posts if p["text"] == "Dubbla vägguttag i sovrum")["quantity"] == 20  # 2 × 10 apartments
    wb = openpyxl.load_workbook(out / "kalkyl.xlsx")
    overview = {r[0].value: r[1].value for r in wb["Översikt"].iter_rows(min_row=5) if r[0].value}
    assert overview["Objekt lästa av kod ur objektlistan (som appen)"] == (
        "10 lägenheter, 3 lokaler, 2 trapphus, 6 trapphusplan"
    )
    assert overview["Modellen avviker från objektlistan för"] == "lägenheter"
    sheet = wb["Objektmodell"]
    heads = [c.value for c in sheet[4]]
    rows = [dict(zip(heads, (c.value for c in row), strict=True)) for row in sheet.iter_rows(min_row=5)]
    floors = next(r for r in rows if r["Objekt"] == "trapphusplan")
    assert floors["Antal"] == 6 and floors["Räknat av"] == "kod (objektlistan)"
    # Summary carries counts only.
    assert "Exempelgatan" not in (out / "summary.json").read_text()


# A requirement per building, and a line that states how many buildings there are.
METER = Block(id="aaaaaa-000013", doc_id=DOC_A, kind="paragraph", text="2 st mätarskåp i varje byggnad.", page=3)
STATED = Block(id="aaaaaa-000014", doc_id=DOC_A, kind="paragraph", text="Projektet omfattar 3 byggnader.", page=3)
METER_REQ = req(
    "Mätarskåp",
    "per_object",
    "st",
    [cite(METER.id, "2 st mätarskåp i varje byggnad")],
    quantity=2,
    per_object="building",
    catalog_id="downlight",
)
STATED_OBJ = obj("building", "stated", [cite(STATED.id, "3 byggnader")], quantity=3)


def _with_houses(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """The list with a building column: the 1000s in house A, the 2000s in house B."""
    out = [dict(r) for r in rows]
    out[1]["G"] = "Hus"
    for r in out:
        if r.get("A", "")[:1] in ("1", "2") and len(r.get("A", "")) == 4:
            r["G"] = "A" if r["A"].startswith("1") else "B"
    return out


def test_a_building_column_groups_apartments_and_a_stairwell_or_type_column_does_not(tmp_path: Path) -> None:
    corpus, cells = _corpus(tmp_path, _sheet(DOC, "Objekt", _with_houses(LIST)))
    (lst,) = read_lists(corpus, cells)
    assert lst.building_column == "G" and {k: len(v) for k, v in lst.buildings().items()} == {"A": 5, "B": 5}
    typed = [dict(r) for r in _with_houses(LIST)]
    typed[1]["G"] = "Hustyp"  # a building type, not a building
    corpus, cells = _corpus(tmp_path / "t", _sheet(DOC, "Objekt", typed))
    (lst,) = read_lists(corpus, cells)
    assert lst.address_column == "D" and lst.building_column is None and lst.buildings() == {}  # "Trapphus" is D


@pytest.mark.parametrize(
    ("path", "houses", "stated", "count", "source"),
    [
        ("stated", True, True, 3, "model"),
        ("derived", True, False, 2, "code"),
        ("assumed", False, False, 1, "assumed"),
    ],
)
def test_a_per_building_requirement_takes_a_stated_then_a_derived_then_an_assumed_building_count(
    data_root: Path, path: str, houses: bool, stated: bool, count: int, source: str
) -> None:
    tender = paths.dev_tender_dir("co-001", "t-0001")
    blocks, cells = _sheet(DOC, "Objekt", _with_houses(LIST) if houses else LIST)
    extra = [METER, STATED] if stated else [METER]
    prep = write_prep(tender / "prep", [*BLOCKS[:12], *extra, *BLOCKS[12:], *blocks], OUTLINE)
    write_cells(prep, cells)
    write_catalog(data_root)

    def handler(params: dict[str, Any]) -> Any:
        if "tools" in params and params["tools"][0]["name"] == "submit_quantities":
            if first_block(params) == "aaaaaa-000001":
                good = GOOD["aaaaaa-000001"]
                records = [*good["records"], METER_REQ, *([STATED_OBJ] if stated else [])]
                return message([submit({"records": records, "set_aside": good.get("set_aside", [])})])
        return good_handler(params)

    summary = run_dev_tender("co-001", "t-0001", sdk=fake_sdk(handler), catalog_name="synthetic", run_id="kalkyl-b")
    result = json.loads((tender / "kalkyl" / "kalkyl-b" / "result.json").read_text())
    building = next(o for o in result["objects"] if o["kind"] == "building")
    assert Decimal(building["count"]) == count and building["source"] == source
    post = next(p for p in result["posts"] if p["name"] == "Mätarskåp")
    assert Decimal(post["quantity"]) == 2 * count and post["status"] == "Prissatt"
    if path == "stated":  # the count the documents state, with its citation; the derived one is a conflict note
        assert building["rows"] == [STATED.id]
        assert building["conflict"] == "Konflikt: 3 enligt underlaget / 2 härledd – underlaget används"
    if path == "derived":  # from the list's building column, labelled derived, with the rows it groups
        assert "härledd: unika värden i kolumn G" in building["basis"] and len(building["rows"]) == 10
        assert any(n.startswith("Härledd: antal byggnader anges inte") for n in post["notes"])
    if path == "assumed":  # at least one, labelled an assumption, inside "varav antagna mängder"
        assert post["level"] == "Antagande" and ASSUMED_COUNT_NOTE.format(many="byggnader") in post["notes"]
        assert Decimal(result["totals"]["assumed_total"]) == Decimal(post["total"])
    assert summary["posts_assumed_object_count"] == (path == "assumed")
    assert (post["level"] == "Antagande") == (path == "assumed")
