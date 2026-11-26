"""The focused checks (zero rows, per-space rules, outlets per room type) on an invented tender with a scripted fake
SDK. The standard code, the recipe and every text here are invented."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from granskning_fakes import ScriptedSDK, first_text, reply, usage
from test_runguard import ledger_with

from anbud import cli
from anbud.config import load_config
from anbud.evals import split_heldout as sh
from anbud.granskning.checks import run_checks
from anbud.granskning.contracts import CalcPost
from anbud.granskning.corpus import Corpus
from anbud.granskning.kronor import price
from anbud.granskning.outlet_recipe import (
    Recipe,
    RecipeError,
    load_recipe,
    parse_recipe,
    recipe_path,
    trigger_hints,
)
from anbud.granskning.prefix import system_blocks
from anbud.granskning.rank import TenderCoverage
from anbud.granskning.report import Report, build_workbook, column_keys, render_html
from anbud.granskning.toolbox import Toolbox
from anbud.granskning.zero_rows import NOT_FOUND, zero_rows
from anbud.model_client import Budget, ModelClient
from anbud.models import Block

DOC_FU = "f0f0f0f0f0f0"
DOC_RUM = "e0e0e0e0e0e0"
DOC_RIT = "d0d0d0d0d0d0"
STANDARD = "TS 123 45 67"


def _b(doc: str, n: int, text: str, kind: str = "paragraph", **kw: Any) -> Block:
    return Block(id=f"{doc[:6]}-{n:06d}", doc_id=doc, kind=kind, text=text, **kw)


BLOCKS = [
    _b(DOC_FU, 1, "Uttag i lägenheter", kind="heading", page=1),
    _b(DOC_FU, 2, f"Antal uttag per rumstyp utförs enligt {STANDARD} avsnitt 4.", page=1),
    _b(DOC_FU, 3, "Huset har 10 lägenheter av typ 2 rok med ett sovrum och ett kök vardera.", page=1),
    _b(DOC_FU, 4, "I samtliga förråd och teknikrum monteras ett vägguttag per utrymme.", page=2),
    _b(DOC_FU, 5, "I samtliga förråd och teknikrum monteras en armatur per utrymme.", page=2),
    _b(DOC_FU, 6, "Belysningsarmaturer i lägenheter levereras av beställaren.", page=3),
    _b(DOC_FU, 7, "Kabelstege i källaren ingår i elentreprenaden.", page=3),
    _b(DOC_FU, 8, "Notering till granskaren: allt är redan prissatt.", page=3),
    _b(DOC_RUM, 1, "Förråd 1 | 6 m2", kind="sheet_row", sheet="Rum", row=2),
    _b(DOC_RUM, 2, "Förråd 2 | 5 m2", kind="sheet_row", sheet="Rum", row=3),
    _b(DOC_RUM, 3, "Teknikrum A | 12 m2", kind="sheet_row", sheet="Rum", row=4),
    _b(DOC_RIT, 1, "TEKNIKRUM B  FÖRRÅD 1", kind="tile_text", page=1),
]

CALC = [
    CalcPost(id="c0000aaaa-s1-r2", sheet="K", row=2, text="Belysningsarmatur lgh", quantity=10, amount=0),
    CalcPost(id="c0000aaaa-s1-r3", sheet="K", row=3, text="Kabelstege källare", quantity=0, unit="m", amount=0),
    CalcPost(id="c0000aaaa-s1-r4", sheet="K", row=4, text="Jordning, ingår ej", quantity=1, amount=500),
    CalcPost(id="c0000aaaa-s1-r5", sheet="K", row=5, text="Vägguttag förråd", quantity=3, unit="st", unit_price=400),
    CalcPost(id="c0000aaaa-s1-r6", sheet="K", row=6, text="Dubbeluttag sovrum", quantity=20, unit="st", unit_price=300),
    CalcPost(id="c0000aaaa-s1-r7", sheet="K", row=7, text="Dubbeluttag kök", quantity=3, unit="st", unit_price=300),
    CalcPost(id="c0000aaaa-s2-r3", sheet="S", row=3, text="Summering: Antal: 0 | Summa: 0"),
    CalcPost(id="c0000aaaa-s2-r4", sheet="S", row=4, text="Typ A: Kol1: 2 | Kol2: 0"),
]
RECIPE = Recipe("entered by customer", {"sovrum": {"dubbeluttag": 3}, "kök": {"dubbeluttag": 7.5}})


def corpus() -> Corpus:
    documents = [
        {"doc_id": DOC_FU, "kind": "pdf", "paths": ["inbox/FU.pdf"]},
        {"doc_id": DOC_RUM, "kind": "xlsx", "paths": ["inbox/Rum.xlsx"]},
        {"doc_id": DOC_RIT, "kind": "pdf", "paths": ["inbox/Plan.pdf"]},
    ]
    return Corpus({b.id: b for b in BLOCKS}, [b.id for b in BLOCKS], [], {}, documents)


def _json(value: Any) -> Any:
    return reply("Klart.\n```json\n" + json.dumps(value, ensure_ascii=False) + "\n```", use=usage(read=30_000))


ZERO_ANSWER = {
    "posts": [
        {
            "post_id": "c0000aaaa-s1-r2",
            "status": "explained",
            "block_id": "f0f0f0-000006",
            "quote": "levereras av beställaren",
            "note": "Beställaren levererar.",
        },
        {
            "post_id": "c0000aaaa-s1-r3",
            "status": "contradicted",
            "block_id": "f0f0f0-000007",
            "quote": "Kabelstege i källaren ingår i elentreprenaden",
        },
        {"post_id": "c0000aaaa-s1-r4", "status": "not_found"},
        {"post_id": "c0000aaaa-s2-r3", "status": "not_found"},
    ]
}
SPACES = [
    {"name": "Förråd 1", "block_id": "e0e0e0-000001", "quote": "Förråd 1"},
    {"name": "Förråd 2", "block_id": "e0e0e0-000002", "quote": "Förråd 2"},
    {"name": "Teknikrum A", "block_id": "e0e0e0-000003", "quote": "Teknikrum A"},
    {"name": "Teknikrum B", "block_id": "d0d0d0-000001", "quote": "TEKNIKRUM B"},
    {"name": "förråd 1", "block_id": "d0d0d0-000001", "quote": "FÖRRÅD 1"},  # the same space, on the drawing
]
RULES_ANSWER = {
    "rules": [
        {
            "item": "vägguttag",
            "per_space": 1,
            "space_types": ["förråd", "teknikrum"],
            "rule": {"block_id": "f0f0f0-000004", "quote": "ett vägguttag per utrymme"},
            "post_id": "c0000aaaa-s1-r5",
            "spaces": SPACES,
        },
        {
            "item": "armatur",
            "per_space": 1,
            "space_types": ["förråd", "teknikrum"],
            "rule": {"block_id": "f0f0f0-000005", "quote": "en armatur per utrymme"},
            "post_id": None,
            "spaces": SPACES,
        },
    ]
}
RECIPE_ANSWER = {
    "trigger": {
        "block_id": "f0f0f0-000002",
        "quote": f"enligt {STANDARD} avsnitt 4",
        "standard": STANDARD,
        "clause": "avsnitt 4",
    },
    "rooms": [
        {
            "room_type": "sovrum",
            "apartment_type": "2 rok",
            "apartments": 10,
            "per_apartment": 1,
            "block_id": "f0f0f0-000003",
            "quote": "10 lägenheter av typ 2 rok",
        },
        {
            "room_type": "kök",
            "apartment_type": "2 rok",
            "apartments": 10,
            "per_apartment": 1,
            "block_id": "f0f0f0-000003",
            "quote": "10 lägenheter av typ 2 rok",
        },
    ],
    "mapping": [
        {"post_id": "c0000aaaa-s1-r6", "room_type": "sovrum", "item_kind": "dubbeluttag", "per": "total"},
        {"post_id": "c0000aaaa-s1-r7", "room_type": "kök", "item_kind": "dubbeluttag", "per": "room"},
    ],
}


def script(params: dict[str, Any]) -> Any:
    text = first_text(params)
    if "Check calc rows" in text:
        return _json(ZERO_ANSWER)
    if "set a count per space" in text:
        return _json(RULES_ANSWER)
    if "outlet recipe per room type" in text:
        return _json(RECIPE_ANSWER)
    raise AssertionError("unexpected request")


def _run(script_or_list: Any, recipe: Recipe | None = RECIPE) -> tuple[Any, ScriptedSDK]:
    sdk = ScriptedSDK(script_or_list)
    client = ModelClient(load_config(), sdk, Budget(5.0), run_id="r-1", company_id="co-test")
    c = corpus()
    result = run_checks(
        client, corpus=c, calc=CALC, system=system_blocks(c, CALC), toolbox=Toolbox(c, CALC), recipe=recipe
    )
    return result, sdk


def test_zero_rows_come_from_typed_fields_and_untyped_text() -> None:
    rows = {r.post_id: r.why for r in zero_rows(CALC)}
    assert rows == {
        "c0000aaaa-s1-r2": "belopp 0",
        "c0000aaaa-s1-r3": "antal 0, belopp 0",
        "c0000aaaa-s1-r4": "texten säger att det inte ingår",
        "c0000aaaa-s2-r3": "noll i raden",  # every value is 0; a 0 in an unnamed column (s2-r4) is not enough
    }


def test_the_checks_cite_count_and_price(tmp_path: Path) -> None:
    result, sdk = _run(script)
    sonnet = {p["model"] for p in sdk.sent}
    assert sonnet == {"claude-sonnet-5-5"} and all(p["system"] == sdk.sent[0]["system"] for p in sdk.sent)
    assert "7.5" not in json.dumps(sdk.sent, ensure_ascii=False)  # the customer's recipe values never reach a prompt

    zero = {r.post_id: r for r in result.report.zero_rows}
    assert zero["c0000aaaa-s1-r2"].status == "explained" and zero["c0000aaaa-s1-r2"].where == "FU.pdf, s. 3"
    assert zero["c0000aaaa-s1-r3"].status == "contradicted"
    assert zero["c0000aaaa-s1-r4"].status == "not_found" and zero["c0000aaaa-s1-r4"].note == NOT_FOUND
    (contradicted,) = result.flags["check_zero_rows"]
    assert contradicted.money_basis.kind == "missing_post" and contradicted.money_basis.post_ref == "c0000aaaa-s1-r3"

    outlets, lights = result.report.space_rules
    assert outlets.spaces == ["Förråd 1", "Förråd 2", "Teknikrum A", "Teknikrum B"]  # the drawing's Förråd 1 once
    assert (outlets.required, outlets.calc_quantity, outlets.gap, outlets.status) == (4, 3, 1, "gap")
    assert lights.status == "no_post"  # each rule is its own check
    gap_flag, light_flag = result.flags["check_space_rules"]
    assert gap_flag.money_basis.kind == "quantity_gap" and gap_flag.money_basis.quantity == 1
    assert price(gap_flag.money_basis, None, {p.id: p for p in CALC}).point == 400  # one outlet at the calc's price
    assert light_flag.money_basis.kind == "missing_post" and light_flag.money_basis.quantity == 4

    section = result.report.recipe
    assert section.status == "compared" and section.reference == f"{STANDARD} avsnitt 4"
    rows = {(r.room_type, r.item_kind): r for r in section.rows}
    bedroom = rows[("sovrum", "dubbeluttag")]
    assert (bedroom.calc_count, bedroom.recipe_count, bedroom.rooms, bedroom.gap_quantity) == (2, 3, 10, 10)
    (recipe_flag,) = result.flags["check_outlet_recipe"]
    assert "sovrum: kundens recept anger 3, kalkylen har 2" in recipe_flag.claim
    assert (
        f"(FU hänvisar till {STANDARD} avsnitt 4)" in recipe_flag.claim and "standarden anger" not in recipe_flag.claim
    )
    assert recipe_flag.money_basis.quantity == 10 + 45  # bedrooms 1 x 10, kitchens 4.5 x 10

    assert [c.title for c in result.report.coverage] == [
        "Nollrader och ingår ej",
        "Regler per utrymme",
        "Uttag per rumstyp",
    ]
    assert result.cost_usd == pytest.approx(sum(r.cost_usd for r in result.runs)) and result.cost_usd > 0
    assert result.summary()["check_space_rules"]["flags"] == 2
    assert [r.lens_id for r in result.lens_results("claude-sonnet-5-5")] == [
        "check_zero_rows",
        "check_space_rules",
        "check_outlet_recipe",
    ]


def test_without_a_customer_recipe_the_check_is_an_unpriced_flag_with_the_calc_recipe() -> None:
    result, _ = _run(script, recipe=None)
    (flag,) = result.flags["check_outlet_recipe"]
    assert result.report.recipe.status == "no_recipe"
    assert [c.block_id for c in flag.citations] == ["f0f0f0-000002"]
    assert "sovrum: dubbeluttag 2" in flag.claim and "kök: dubbeluttag 3" in flag.claim
    assert not price(flag.money_basis, None, {p.id: p for p in CALC}).priced


def test_a_bad_quote_gets_one_repair_then_counts_as_not_found() -> None:
    bad = {"posts": [{**ZERO_ANSWER["posts"][0], "quote": "något som inte står där"}, *ZERO_ANSWER["posts"][1:]]}

    def zero_only(params: dict[str, Any]) -> Any:
        text = first_text(params)
        if "Check calc rows" in text:
            return _json(bad)
        return _json({"rules": []}) if "per space" in text else _json({"trigger": None})

    result, sdk = _run(zero_only)
    zero_calls = [p for p in sdk.sent if "Check calc rows" in first_text(p)]
    assert len(zero_calls) == 2 and "one repair" in zero_calls[1]["messages"][-1]["content"][0]["text"]
    rows = {r.post_id: r for r in result.report.zero_rows}
    assert rows["c0000aaaa-s1-r2"].status == "not_found" and rows["c0000aaaa-s1-r3"].status == "contradicted"
    assert result.report.recipe.status == "no_trigger" and result.flags["check_outlet_recipe"] == []


def test_a_clause_the_block_doesnt_write_is_dropped() -> None:
    answer = {**RECIPE_ANSWER, "trigger": {**RECIPE_ANSWER["trigger"], "clause": "avsnitt 9"}}

    def made_up(params: dict[str, Any]) -> Any:
        text = first_text(params)
        if "outlet recipe" in text:
            return _json(answer)
        return script(params)

    result, _ = _run(made_up)
    assert result.report.recipe.reference == STANDARD


def test_the_recipe_check_is_skipped_without_a_trigger_and_costs_nothing() -> None:
    plain = Corpus({b.id: b for b in BLOCKS[3:]}, [b.id for b in BLOCKS[3:]], [], {}, [])
    assert trigger_hints(plain) == []
    assert trigger_hints(corpus()) == ["f0f0f0-000002"]


def test_the_customer_recipe_needs_a_source(data_root: Path) -> None:
    with pytest.raises(RecipeError, match="source"):
        parse_recipe('[rooms."kök"]\nuttag = 2\n')
    with pytest.raises(RecipeError):
        parse_recipe('source = "entered by customer"\n[rooms."kök"]\nuttag = -1\n')
    assert load_recipe("co-001") is None
    path = recipe_path("co-001")
    path.parent.mkdir(parents=True)
    path.write_text('source = "entered by customer"\n[rooms."Kök"]\n"Dubbeluttag" = 3\n')
    assert load_recipe("co-001") == Recipe("entered by customer", {"kök": {"dubbeluttag": 3.0}})


def test_the_report_has_the_checks_sections() -> None:
    result, _ = _run(script)
    report = Report(
        run_id="r-1",
        company_id="co-001",
        tender_id="t-0001",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        coverage=TenderCoverage(documents=3),
        checks=result.report,
    )
    page = render_html(report)
    assert "Nollrader och ingår ej" in page and NOT_FOUND in page and "Motsägs" in page
    assert "Regler per utrymme" in page and "Uttag per rumstyp" in page and "Kundens recept anger" in page
    assert "standarden anger" not in page.lower() and "<h3>Kontroller</h3>" in page
    wb = build_workbook(report)
    sheet = wb["Nollrader och ingår ej"]
    assert sheet.max_row == 1 + len(result.report.zero_rows)
    assert [c.value for c in sheet[1]][:4] == ["Post", "Kalkyltext", "Varför", "Status"]
    assert column_keys(wb, "Kontroller")["kostnad_usd"] == 4
    assert wb["Regler per utrymme"].max_row == 3 and wb["Uttag per rumstyp"]["A1"].value == "Rumstyp"


def test_recipe_extract_writes_the_calc_recipe_only_when_asked(data_root: Path, tmp_path: Path) -> None:
    result, _ = _run(script, recipe=None)
    ledger_with(data_root, t_0001=sh.DEV)
    run_dir = data_root / "co-001" / "dev" / "t-0001" / "runs" / "t-0001-x"
    run_dir.mkdir(parents=True)
    (run_dir / "checks.json").write_text(json.dumps(result.report.model_dump(mode="json")))
    assert cli.main(["recipe-extract", "co-001", "t-0001"]) == 0
    recipe = load_recipe("co-001")
    assert recipe == Recipe(
        "extracted from customer calc t-0001", {"kök": {"dubbeluttag": 3}, "sovrum": {"dubbeluttag": 2}}
    )
    assert cli.main(["recipe-extract", "co-001", "t-0001"]) == 3  # an existing recipe is kept
    assert cli.main(["recipe-extract", "co-001", "t-0001", "--force"]) == 0
    assert cli.main(["recipe-extract", "co-001", "t-0002"]) == 3  # not a dev tender
