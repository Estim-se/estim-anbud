"""Lines that say something isn't part of the contract, and lines with a zero quantity, always end in the calc with
their citation: as a post, set aside by the model, set aside by code, or listed as not handled."""

from __future__ import annotations

import json
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
    req,
    submit,
    write_catalog,
    write_prep,
)

from anbud import paths
from anbud.granskning.corpus import Corpus
from anbud.kalkyl.coverage import NOT_INCLUDED_REASON, OTHER_TRADE_CHECK
from anbud.kalkyl.lines import names_other_party, not_included_line_ids, says_not_included
from anbud.kalkyl.pipeline import NOT_INCLUDED_NOTE
from anbud.kalkyl.run import run_dev_tender
from anbud.models import Block


def _block(n: int, kind: str, text: str) -> Block:
    return Block(id=f"{DOC_A[:6]}-{n:06d}", doc_id=DOC_A, kind=kind, text=text, page=3)  # type: ignore[arg-type]


EXTRA = [
    _block(13, "paragraph", "Fiberanslutning ingår ej."),  # no number: not a quantity line, not a scope line
    _block(14, "paragraph", "Laddboxar 2 st i garage ingår inte i denna entreprenad."),  # a quantity line
    _block(15, "paragraph", "Brandlarm 0 st i förråd."),
    _block(16, "paragraph", "Porttelefon 3 st, montage ingår ej."),
]


@pytest.mark.parametrize(
    ("text", "says"),
    [
        ("Fiberanslutning ingår ej.", True),
        ("Montage ingår inte", True),
        ("Ingår icke i entreprenaden", True),
        ("Rivning ej i denna entreprenad", True),
        ("Utförs ej i entreprenaden", True),
        ("Befintlig armatur utgår", False),  # removal work, not an exclusion
        ("Ingående material levereras av entreprenören", False),
        ("Allt material ingår", False),
    ],
)
def test_not_included_is_read_from_generic_contract_words(text: str, says: bool) -> None:
    assert says_not_included(text) is says


def test_lines_under_a_not_included_heading_count_but_the_heading_does_not(tmp_path: Path) -> None:
    blocks = [
        Block(id="cccccc-000001", doc_id="cccccccccccc", kind="heading", text="4 Ingår ej", page=1),
        Block(id="cccccc-000002", doc_id="cccccccccccc", kind="paragraph", text="Fiberanslutning", page=1),
        Block(id="cccccc-000003", doc_id="cccccccccccc", kind="heading", text="5 Belysning", page=1),
        Block(id="cccccc-000004", doc_id="cccccccccccc", kind="paragraph", text="Armaturer enligt ritning", page=1),
    ]
    outline = [
        {"doc_id": "cccccccccccc", "level": 1, "title": "4 Ingår ej", "block_id": "cccccc-000001", "page": 1},
        {"doc_id": "cccccccccccc", "level": 1, "title": "5 Belysning", "block_id": "cccccc-000003", "page": 1},
    ]
    corpus = Corpus.load(write_prep(tmp_path / "prep", blocks, outline))
    assert not_included_line_ids(corpus) == {"cccccc-000002"}


def _handler(extra_records: list[dict[str, Any]], set_aside: list[dict[str, Any]] | None = None):
    def handler(params: dict) -> Any:
        if "tools" in params and params["tools"][0]["name"] == "submit_quantities":
            if first_block(params) == "aaaaaa-000001":
                good = GOOD["aaaaaa-000001"]
                payload = {"records": [*good["records"], *extra_records], "set_aside": set_aside or []}
                return message([submit(payload)])
        return good_handler(params)

    return handler


@pytest.fixture
def tender(data_root: Path) -> Path:
    tender = paths.dev_tender_dir("co-001", "t-0001")
    write_prep(tender / "prep", [*BLOCKS[:12], *EXTRA, *BLOCKS[12:]], OUTLINE)
    write_catalog(data_root)
    return tender


def _run(handler: Any) -> dict[str, Any]:
    return run_dev_tender("co-001", "t-0001", sdk=fake_sdk(handler), catalog_name="synthetic", run_id="kalkyl-ni")


def _rows(ws: Any) -> list[dict[str, Any]]:
    heads = [c.value for c in ws[4]]
    return [dict(zip(heads, (c.value for c in row), strict=True)) for row in ws.iter_rows(min_row=5) if row[0].value]


def test_unhandled_not_included_lines_are_set_aside_by_code_with_their_citation(tender: Path) -> None:
    zero = req("Brandlarm", "stated", "st", [cite("aaaaaa-000015", "Brandlarm 0 st")], quantity=0)
    summary = _run(_handler([zero]))
    assert summary["not_included_lines"] == 2 + 1  # blocks 13 and 14, and 16 (cited by no record here either)
    assert summary["not_included_set_aside_by_code"] == 3 and summary["lines_unresolved"] == 0
    out = tender / "kalkyl" / "kalkyl-ni"
    result = json.loads((out / "result.json").read_text())
    by_block = {u["block_id"]: u for u in result["uncovered"]}
    assert by_block["aaaaaa-000013"]["line"] == "not_included"  # found by its words, though no marker named it
    assert by_block["aaaaaa-000014"]["line"] == "quantity"  # a quantity line the model left: set aside, not lost
    for block_id in ("aaaaaa-000013", "aaaaaa-000014", "aaaaaa-000016"):
        u = by_block[block_id]
        assert (
            u["set_aside_by_code"] and u["set_aside_kind"] == "not_this_contract" and u["reason"] == NOT_INCLUDED_REASON
        )
    wb = openpyxl.load_workbook(out / "kalkyl.xlsx")
    reasoned = {r["Källa (block)"]: r for r in _rows(wb["Bortvalt med skäl"])}
    assert reasoned["aaaaaa-000013"]["Text"] == "Fiberanslutning ingår ej." and reasoned["aaaaaa-000013"]["Typ"] == (
        "Rad om något som inte ingår"
    )
    assert "aaaaaa-000013" not in {r["Källa (block)"] for r in _rows(wb["Ej omhändertaget"])}

    # A zero quantity stays a post with its citation; it is never dropped as "no quantity".
    posts = {p["name"]: p for p in result["posts"]}
    assert posts["Brandlarm"]["quantity"] in (0, "0") and posts["Brandlarm"]["status"] != "Saknar mängd"
    calc = {r["Post"]: r for r in _rows(wb["Kalkyl"]) if r.get("Post")}
    assert calc["Brandlarm"]["Källa (block)"] == "aaaaaa-000015" and "0 st" in calc["Brandlarm"]["Citat"]


def test_the_models_set_aside_comes_first_and_a_zero_line_nobody_handled_stays_not_handled(tender: Path) -> None:
    aside = [{"block_ids": ["aaaaaa-000013"], "kind": "other_trade", "reason": "the client orders it"}]
    summary = _run(_handler([], aside))
    result = json.loads((tender / "kalkyl" / "kalkyl-ni" / "result.json").read_text())
    by_block = {u["block_id"]: u for u in result["uncovered"]}
    assert by_block["aaaaaa-000013"]["set_aside_by_model"] and by_block["aaaaaa-000013"]["set_aside_kind"] == (
        "other_trade"
    )
    assert summary["not_included_set_aside_by_code"] == 2  # 14 and 16
    assert by_block["aaaaaa-000015"]["set_aside_kind"] is None and summary["lines_unresolved"] == 1  # the zero line


def test_a_post_citing_a_not_included_line_keeps_its_price_and_gets_a_note(tender: Path) -> None:
    phone = req(
        "Porttelefon", "stated", "st", [cite("aaaaaa-000016", "Porttelefon 3 st")], quantity=3, catalog_id="downlight"
    )
    summary = _run(_handler([phone]))
    assert summary["posts_citing_not_included"] == 1
    result = json.loads((tender / "kalkyl" / "kalkyl-ni" / "result.json").read_text())
    post = next(p for p in result["posts"] if p["name"] == "Porttelefon")
    assert NOT_INCLUDED_NOTE in post["notes"] and post["status"] == "Prissatt"
    assert "aaaaaa-000016" not in {u["block_id"] for u in result["uncovered"]}  # used by the post


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("Rörarbeten utförs av VVS-entreprenören.", True),
        ("Ventilationsaggregat levereras av V-entreprenören", True),
        ("Byggentreprenören svarar för håltagning", True),
        ("Spis och kyl monteras av beställaren", True),
        ("Arbeten i undercentral ingår ej", True),
        ("Gränsdragning mot sidoentreprenad enligt bilaga", True),
        ("Matning till ventilationsaggregat", False),  # another trade's equipment, fed by this contract
        ("Spis, ugn och diskmaskin i varje kök", False),
        ("Befintlig armatur demonteras", False),
        ("Entreprenören ansvarar för samordning", False),  # this contract's own contractor
        ("Elentreprenören utför provning", False),
    ],
)
def test_another_party_is_read_from_generic_trade_and_boundary_words(text: str, names: bool) -> None:
    assert names_other_party(text) is names


def test_an_other_trade_set_aside_needs_its_line_to_name_another_party(tender: Path) -> None:
    aside = [
        {"block_ids": ["aaaaaa-000013"], "kind": "other_trade", "reason": "the operator connects it"},  # "ingår ej"
        {"block_ids": ["aaaaaa-000015"], "kind": "other_trade", "reason": "the builder does it"},  # names no one
    ]
    summary = _run(_handler([], aside))
    result = json.loads((tender / "kalkyl" / "kalkyl-ni" / "result.json").read_text())
    by_block = {u["block_id"]: u for u in result["uncovered"]}
    assert by_block["aaaaaa-000013"]["set_aside_kind"] == "other_trade"  # the line names a contract boundary
    unchecked = by_block["aaaaaa-000015"]
    assert unchecked["set_aside_kind"] == "unclear" and unchecked["other_trade_unchecked"]
    assert unchecked["reason"] == OTHER_TRADE_CHECK.format(reason="the builder does it")
    assert summary["other_trade_unchecked"] == 1
    wb = openpyxl.load_workbook(tender / "kalkyl" / "kalkyl-ni" / "kalkyl.xlsx")
    assert "aaaaaa-000015" in {r["Källa (block)"] for r in _rows(wb["Ej omhändertaget"])}
