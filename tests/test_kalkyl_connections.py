"""Powered equipment another party supplies still gets its connection in the calc, and an unconfirmed demolition line
asks whether the existing electrical installation is in scope. Synthetic lines only."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

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
from anbud.kalkyl.connections import CONNECTION_NOTE, QUESTION, equipment_in, is_demolition
from anbud.kalkyl.coverage import DEMOLITION_QUESTION, OTHER_TRADE_CHECK
from anbud.kalkyl.run import run_dev_tender
from anbud.models import Block


def _block(n: int, text: str) -> Block:
    return Block(id=f"{DOC_A[:6]}-{n:06d}", doc_id=DOC_A, kind="paragraph", text=text, page=3)


APPLIANCES = _block(20, "Spis och diskmaskin i varje kök levereras av beställaren och monteras.")  # names the client
FLOOR_HEAT = _block(21, "Golvvärme i badrum.")  # names no one
LIFT = _block(22, "Hiss med maskinrum.")  # no count, no room type
DEMOLISH = _block(23, "Befintlig belysning i trapphus rivs.")
PUMPS = _block(24, "Cirkulationspump 2 st.")
PAINT = _block(25, "Målning av väggar.")  # no powered equipment
FRIDGE = _block(26, "Kyl och frys levereras av beställaren.")
FRIDGE_OUTLET = _block(27, "Uttag för kyl och frys i varje kök.")
EXTRA = [APPLIANCES, FLOOR_HEAT, LIFT, DEMOLISH, PUMPS, PAINT, FRIDGE, FRIDGE_OUTLET]


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("Spis, ugn och diskmaskin i varje kök", ["spis och häll", "ugn", "diskmaskin"]),
        ("Mikrovågsugn ingår", ["mikrovågsugn"]),
        ("Spisfläkt monteras av köksleverantören", ["spiskåpa och köksfläkt"]),
        ("Värmepump och cirkulationspump i undercentral", ["pump", "värmepump", "undercentral"]),
        ("Garageport med portautomatik", ["portautomatik"]),
        ("Spisvakt i varje kök", []),  # an electrical item of this contract, not another party's equipment
        ("Målning av väggar", []),
        ("Hällristning vid entrén", []),  # a word that only starts like an appliance
    ],
)
def test_powered_equipment_is_read_as_generic_kinds(text: str, names: list[str]) -> None:
    assert equipment_in(text) == names


def test_demolition_is_read_from_generic_words() -> None:
    assert is_demolition("Befintliga installationer rivs") and is_demolition("Demontering av armaturer")
    assert not is_demolition("Nya armaturer monteras")


@pytest.fixture
def tender(data_root: Path) -> Path:
    tender = paths.dev_tender_dir("co-001", "t-0001")
    write_prep(tender / "prep", [*BLOCKS[:12], *EXTRA, *BLOCKS[12:]], OUTLINE)
    write_catalog(data_root)
    return tender


def _aside(block: Block, reason: str) -> dict[str, Any]:
    return {"block_ids": [block.id], "kind": "other_trade", "reason": reason}


def _handler(params: dict[str, Any]) -> Any:
    if "tools" in params and params["tools"][0]["name"] == "submit_quantities":
        if first_block(params) == "aaaaaa-000001":
            outlet = req(
                "Uttag kyl/frys",
                "per_object",
                "st",
                [cite(FRIDGE_OUTLET.id, "Uttag för kyl och frys i varje kök")],
                per_object="kitchen",
            )
            asides = [
                _aside(b, "another trade's work")
                for b in (APPLIANCES, FLOOR_HEAT, LIFT, DEMOLISH, PUMPS, PAINT, FRIDGE)
            ]
            return message([submit({"records": [*GOOD["aaaaaa-000001"]["records"], outlet], "set_aside": asides})])
    return good_handler(params)


def test_equipment_set_aside_as_another_trades_gets_a_connection_post_and_keeps_its_set_aside(tender: Path) -> None:
    summary = run_dev_tender("co-001", "t-0001", sdk=fake_sdk(_handler), catalog_name="synthetic", run_id="kalkyl-c")
    result = json.loads((tender / "kalkyl" / "kalkyl-c" / "result.json").read_text())
    posts = {p["name"]: p for p in result["posts"]}
    connections = {v["id"]: v for v in result["connections"]}

    # Spis and diskmaskin: one connection each, per kitchen, assumed one each, citing the line.
    for name in ("Anslutning spis och häll", "Anslutning diskmaskin"):
        post = posts[name]
        assert post["per_object"] == ["kitchen"] and post["level"] == "Antagande" and CONNECTION_NOTE in post["notes"]
        assert {b for r in post["records"] for b in connections[r]["block_ids"]} == {APPLIANCES.id}
    assert posts["Anslutning golvvärme"]["per_object"] == ["bathroom"]
    assert Decimal(posts["Anslutning pump"]["quantity"]) == 2  # the number the line writes
    lift = posts["Anslutning hiss"]
    assert lift["quantity"] is None and lift["status"] == "Saknar mängd" and QUESTION in lift["notes"]
    # Kyl och frys already has a record in the calc: no second connection. Painting and demolition get none.
    assert "Anslutning kyl och frys" not in posts
    assert not any(v["block_ids"] == [PAINT.id] or v["block_ids"] == [DEMOLISH.id] for v in connections.values())
    assert summary["connection_posts"] == 5 and summary["connection_lines"] == 4

    # The supply stays set aside: as another trade's work where the line names the party, unclear where it doesn't.
    by_block = {u["block_id"]: u for u in result["uncovered"]}
    assert by_block[APPLIANCES.id]["set_aside_kind"] == "other_trade"
    assert by_block[FLOOR_HEAT.id]["set_aside_kind"] == "unclear"
    assert by_block[FLOOR_HEAT.id]["reason"] == OTHER_TRADE_CHECK.format(reason="another trade's work")


def test_an_unconfirmed_demolition_line_asks_about_the_existing_electrical_installation(tender: Path) -> None:
    run_dev_tender("co-001", "t-0001", sdk=fake_sdk(_handler), catalog_name="synthetic", run_id="kalkyl-c")
    result = json.loads((tender / "kalkyl" / "kalkyl-c" / "result.json").read_text())
    by_block = {u["block_id"]: u for u in result["uncovered"]}
    assert by_block[DEMOLISH.id]["set_aside_kind"] == "unclear"
    assert by_block[DEMOLISH.id]["reason"] == DEMOLITION_QUESTION.format(reason="another trade's work")
