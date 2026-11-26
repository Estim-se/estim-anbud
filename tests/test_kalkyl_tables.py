"""Code reads tables: the cells sidecar, layout checks, row reading, and a run where the model names the columns of
the apartment list once instead of writing a record per row. Synthetic, $0."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from kalkyl_synthetic import (
    APARTMENT_TABLE,
    BLOCKS,
    GOOD,
    OUTLINE,
    fake_sdk,
    first_block,
    good_handler,
    message,
    sheet_cells,
    submit,
    write_catalog,
    write_cells,
    write_prep,
)

from anbud import paths
from anbud.granskning.corpus import Corpus
from anbud.kalkyl.lines import make_chunks
from anbud.kalkyl.records import TableLayout
from anbud.kalkyl.run import run_dev_tender
from anbud.kalkyl.tables import check_layout, extend_layout, load_cells, number_and_unit, read_table
from anbud.models import Block

DOC_C = "cccccccccccc"
SCHEDULE_ROWS = [
    "Pos | Benämning | Typ | Antal | Enhet",
    "1 | Vägguttag dubbelt | U2 | 24 | st",
    "2 | Kabelstege | 1 200 | m",
    "3 | Armatur | L9 | ca tio | st",
]
SCHEDULE = [
    Block(id=f"cccccc-{n:06d}", doc_id=DOC_C, kind="sheet_row", text=t, sheet="Mängder", row=n)
    for n, t in enumerate(SCHEDULE_ROWS, 1)
]
SCHEDULE_CELLS = {
    "cccccc-000001": [("A", "Pos"), ("B", "Benämning"), ("C", "Typ"), ("D", "Antal"), ("E", "Enhet")],
    "cccccc-000002": [("A", "1"), ("B", "Vägguttag dubbelt"), ("C", "U2"), ("D", "24"), ("E", "st")],
    "cccccc-000003": [("A", "2"), ("B", "Kabelstege"), ("D", "1 200"), ("E", "m")],  # no type: column C is empty
    "cccccc-000004": [("A", "3"), ("B", "Armatur"), ("C", "L9"), ("D", "ca tio"), ("E", "st")],
}
SCHEDULE_TABLE = {
    "header_block_id": "cccccc-000001",
    "first_block_id": "cccccc-000002",
    "last_block_id": "cccccc-000004",
    "row_kind": "requirement",
    "scope": "included",
    "columns": [
        {"col": "B", "role": "description"},
        {"col": "C", "role": "type_code"},
        {"col": "D", "role": "quantity"},
        {"col": "E", "role": "unit"},
    ],
}


def _corpus(tmp_path: Path, blocks: list[Block], outline: list[dict] | None = None) -> Corpus:
    return Corpus.load(write_prep(tmp_path / "prep", blocks, outline or []))


def _schedule_cells() -> dict:
    return {b: [{"col": c, "text": t, "numeric": False} for c, t in row] for b, row in SCHEDULE_CELLS.items()}


def test_numbers_and_units_in_a_cell() -> None:
    assert number_and_unit("24") == (24.0, None)
    assert number_and_unit("1 200 m") == (1200.0, "m")
    assert number_and_unit("2,5") == (2.5, None)
    assert number_and_unit("ca tio") == (None, None)


def test_a_schedule_is_read_row_by_row_by_column_letter(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path, SCHEDULE)
    write_cells(tmp_path / "prep", _schedule_cells())
    cells = load_cells(tmp_path / "prep" / "cells.jsonl")
    chunk = make_chunks(corpus, 12000, 30)[0]
    layout = TableLayout.model_validate(SCHEDULE_TABLE)
    assert check_layout(layout, chunk, corpus, cells) is None
    rows, unread = read_table(layout, chunk, corpus, cells, set())
    got = {b: (r.what, r.type_code, r.quantity, r.unit) for b, r in rows}
    assert got == {
        "cccccc-000002": ("Vägguttag dubbelt", "U2", 24.0, "st"),
        "cccccc-000003": ("Kabelstege", None, 1200.0, "m"),  # an empty cell doesn't shift the columns
    }
    assert unread == {"cccccc-000004": "mängdcellen är inte ett tal"}
    assert rows[0][1].citations[0].quote == SCHEDULE[1].text  # the whole row is the evidence


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"header_block_id": "zzz"}, "header block"),
        ({"first_block_id": "cccccc-000004", "last_block_id": "cccccc-000002"}, "first before last"),
        ({"columns": [{"col": "Q", "role": "description"}, {"col": "D", "role": "quantity"}]}, "column Q"),
        ({"columns": [{"col": "B", "role": "description"}]}, "needs a description and a quantity"),
        ({"row_kind": "object", "object_kind": None}, "object table needs"),
        (
            {
                "columns": [
                    {"col": "B", "role": "description"},
                    {"col": "D", "role": "quantity"},
                    {"col": "C", "role": "contains"},
                ]
            },
            "contains needs object_kind",
        ),
    ],
)
def test_a_layout_that_doesnt_fit_is_refused(tmp_path: Path, change: dict, problem: str) -> None:
    corpus = _corpus(tmp_path, SCHEDULE)
    write_cells(tmp_path / "prep", _schedule_cells())
    cells = load_cells(tmp_path / "prep" / "cells.jsonl")
    layout = TableLayout.model_validate({**SCHEDULE_TABLE, **change})
    assert problem in (check_layout(layout, make_chunks(corpus, 12000, 30)[0], corpus, cells) or "")


def test_rows_without_cells_cant_be_read_as_a_table(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path, SCHEDULE)
    layout = TableLayout.model_validate(SCHEDULE_TABLE)
    assert "one by one" in (check_layout(layout, make_chunks(corpus, 12000, 30)[0], corpus, {}) or "")


@pytest.fixture
def tender(data_root: Path) -> Path:
    tender = paths.dev_tender_dir("co-001", "t-0001")
    write_prep(tender / "prep", BLOCKS, OUTLINE)
    write_cells(tender / "prep", sheet_cells())
    write_catalog(data_root)
    return tender


def _table_handler(table: dict[str, Any], also_rows: bool = False):
    def handler(params: dict) -> Any:
        if (
            "tools" in params
            and params["tools"][0]["name"] == "submit_quantities"
            and first_block(params) == "bbbbbb-000001"
        ):
            payload = {
                "records": GOOD["bbbbbb-000001"]["records"] if also_rows else [],
                "tables": [table],
                "set_aside": [{"block_ids": ["bbbbbb-000005"], "kind": "total_or_repeat", "reason": "summary row"}],
            }
            return message([submit(payload)])
        return good_handler(params)

    return handler


def test_a_run_reads_the_apartment_list_from_one_table_layout(tender: Path) -> None:
    sdk = fake_sdk(_table_handler(APARTMENT_TABLE, also_rows=True))
    summary = run_dev_tender("co-001", "t-0001", sdk=sdk, catalog_name="synthetic", run_id="kalkyl-t")
    assert summary["tables"] == 1 and summary["table_rows_read"] == 3 and summary["table_rows_unread"] == 0
    assert summary["records_superseded_by_tables"] == 3  # the model's own row records give way to the table
    assert summary["posts_priced"] == 4 and summary["lines_not_handled"] == 1  # the summary row, set aside
    result = json.loads((tender / "kalkyl" / "kalkyl-t" / "result.json").read_text())
    objects = {o["kind"]: o["count"] for o in result["objects"]}
    assert objects == {
        "apartment": "3",
        "room": "9",
        "kitchen": "3",
        "bathroom": "3",
        "living_room": "3",  # three rooms are a living room and two bedrooms
        "bedroom": "6",
    }
    posts = json.loads((tender / "kalkyl" / "kalkyl-t" / "posts.json").read_text())
    assert next(p for p in posts if p["text"] == "Dubbla vägguttag i sovrum")["quantity"] == 6
    sent = [p for p in sdk.messages.sent if first_block(p) == "bbbbbb-000001"][0]
    assert 'cols="A,B,C,D"' in sent["messages"][0]["content"]


def test_a_bad_layout_goes_back_once_and_its_rows_stay_visible(tender: Path) -> None:
    bad = {**APARTMENT_TABLE, "columns": [{"col": "Z", "role": "id"}]}
    sdk = fake_sdk(_table_handler(bad))
    summary = run_dev_tender("co-001", "t-0001", sdk=sdk, catalog_name="synthetic", run_id="kalkyl-u")
    repairs = [p for p in sdk.messages.sent if first_block(p) == "bbbbbb-000001" and len(p["messages"]) == 3]
    assert repairs and "table 1: column Z" in repairs[0]["messages"][-1]["content"][0]["content"]
    assert summary["tables"] == 0 and summary["lines_not_handled"] == 4  # three apartment rows and the summary row


DOC_D = "dddddddddddd"
LIST_ROWS = ["Lgh | Typ | Plan"] + [f"10{n} | {2 + n % 2} rok | {n}" for n in range(1, 8)] + ["Summa | 7 lgh | "]
LIST = [
    Block(id=f"dddddd-{n:06d}", doc_id=DOC_D, kind="sheet_row", text=t, sheet="Lista", row=n)
    for n, t in enumerate(LIST_ROWS, 1)
]


def _list_cells() -> dict:
    out = {}
    for block in LIST:
        cols = [c.strip() for c in block.text.split("|")]
        out[block.id] = [{"col": "ABC"[i], "text": t, "numeric": False} for i, t in enumerate(cols) if t]
    return out


def _list_layout(last: int) -> TableLayout:
    return TableLayout.model_validate(
        {
            "header_block_id": "dddddd-000001",
            "first_block_id": "dddddd-000002",
            "last_block_id": f"dddddd-{last:06d}",
            "row_kind": "object",
            "object_kind": "apartment",
            "scope": "included",
            "columns": [{"col": "A", "role": "id"}, {"col": "B", "role": "contains", "object_kind": "room"}],
        }
    )


def test_a_table_the_model_ends_early_is_extended_over_rows_of_the_same_shape(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path, LIST)
    write_cells(tmp_path / "prep", _list_cells())
    cells = load_cells(tmp_path / "prep" / "cells.jsonl")
    chunk = make_chunks(corpus, 12000, 30)[0]
    layout, added = extend_layout(_list_layout(5), chunk, corpus, cells, set())
    assert added == 3 and layout.last_block_id == "dddddd-000008"  # the total row has another shape: not read
    rows, _ = read_table(layout, chunk, corpus, cells, set())
    assert len(rows) == 7
    stopped, added = extend_layout(_list_layout(5), chunk, corpus, cells, {"dddddd-000007"})
    assert added == 1 and stopped.last_block_id == "dddddd-000006"  # a row the model handled stops it
    whole, added = extend_layout(_list_layout(8), chunk, corpus, cells, set())
    assert added == 0 and whole.last_block_id == "dddddd-000008"


def test_a_short_table_without_a_clear_body_shape_is_not_extended(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path, LIST)
    write_cells(tmp_path / "prep", _list_cells())
    cells = load_cells(tmp_path / "prep" / "cells.jsonl")
    chunk = make_chunks(corpus, 12000, 30)[0]
    layout, added = extend_layout(_list_layout(3), chunk, corpus, cells, set())  # two rows: no body shape yet
    assert added == 0 and layout.last_block_id == "dddddd-000003"
