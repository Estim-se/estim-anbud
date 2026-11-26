"""Review fixes for the focused checks, on the invented tender of test_granskning_checks: check flags keep their own
money, a broken recipe or a failing check never stops the reports, unknown counts stay unpriced, and the checks skip
when the run already stopped or only selected lenses ran."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from granskning_fakes import first_text
from test_granskning_checks import (
    CALC,
    RECIPE,
    RECIPE_ANSWER,
    RULES_ANSWER,
    ZERO_ANSWER,
    _json,
    corpus,
    script,
)

from anbud.config import load_config
from anbud.granskning import pipeline
from anbud.granskning.check_dry import checks_line
from anbud.granskning.check_loop import STOP_NAMES
from anbud.granskning.checks_report import checks_html
from anbud.granskning.contracts import CalcPost, LensResult, fingerprint
from anbud.granskning.kronor import price
from anbud.granskning.loop import FlagSubmission, LoopOutcome
from anbud.granskning.mapping import MapResult
from anbud.granskning.merge import items_from_results, merge
from anbud.granskning.outlet_recipe import Recipe, RecipeError, load_recipe, recipe_path
from anbud.granskning.run import GranskningResult
from anbud.granskning.toolbox import Toolbox
from anbud.granskning.zero_rows import zero_reason
from anbud.model_client import Budget, ModelClient
from anbud.models import Citation, Flag, MoneyBasis
from anbud.store import RunStore

POSTS = {p.id: p for p in CALC}
EXTRA = [
    CalcPost(id="c0000aaaa-s1-r8", sheet="K", row=8, text="Ljuspunkt sovrum", quantity=10, unit="st", unit_price=200),
    CalcPost(id="c0000aaaa-s1-r9", sheet="K", row=9, text="Armatur förråd", quantity=2, unit="st", unit_price=900),
    CalcPost(id="c0000aaaa-s1-r10", sheet="K", row=10, text="Kanalisation källare", quantity=0, unit="m", amount=0),
    CalcPost(id="c0000aaaa-s1-r11", sheet="K", row=11, text="Uttag förråd, se bilaga"),  # no quantity at all
    CalcPost(id="c0000aaaa-s1-r12", sheet="K", row=12, text="Dubbeluttag per sovrum", quantity=2, unit_price=300),
]
CALC2 = [*CALC, *EXTRA]
POSTS2 = {p.id: p for p in CALC2}


def _answers(**over: Any) -> Any:
    answers = {"zero": ZERO_ANSWER, "rules": RULES_ANSWER, "recipe": RECIPE_ANSWER} | over

    def answer(params: dict[str, Any]) -> Any:
        text = first_text(params)
        if "Check calc rows" in text:
            return _json(answers["zero"])
        if "set a count per space" in text:
            return _json(answers["rules"])
        return _json(answers["recipe"])

    return answer


def _run2(answer: Any, recipe: Recipe | None = RECIPE) -> tuple[Any, Any]:
    from granskning_fakes import ScriptedSDK

    from anbud.granskning.checks import run_checks
    from anbud.granskning.prefix import system_blocks

    sdk = ScriptedSDK(answer)
    client = ModelClient(load_config(), sdk, Budget(5.0), run_id="r-1", company_id="co-test")
    c = corpus()
    result = run_checks(
        client, corpus=c, calc=CALC2, system=system_blocks(c, CALC2), toolbox=Toolbox(c, CALC2), recipe=recipe
    )
    return result, sdk


# 1. Check flags with the same citations and claim type keep their own item and money basis.


def test_check_flags_on_the_same_citations_stay_separate_items_each_priced() -> None:
    zero = {
        "posts": [
            *ZERO_ANSWER["posts"],
            {  # a second contradicted row citing the very block the first one cites
                "post_id": "c0000aaaa-s1-r10",
                "status": "contradicted",
                "block_id": "f0f0f0-000007",
                "quote": "Kabelstege i källaren ingår i elentreprenaden",
            },
        ]
    }
    rule = RULES_ANSWER["rules"][0]
    rules = {"rules": [rule, {**rule, "item": "armatur", "post_id": "c0000aaaa-s1-r9"}]}  # same block, same spaces
    recipe = Recipe("entered by customer", {"sovrum": {"dubbeluttag": 3, "ljuspunkt": 2}})
    mapping = [*RECIPE_ANSWER["mapping"]]
    mapping.append({"post_id": "c0000aaaa-s1-r8", "room_type": "sovrum", "item_kind": "ljuspunkt", "per": "total"})
    answer = _answers(zero=zero, rules=rules, recipe={**RECIPE_ANSWER, "mapping": mapping})
    result, _ = _run2(answer, recipe)

    flags = [f for fs in result.flags.values() for f in fs]
    assert len({fingerprint(f) for f in flags}) == len(flags) == 2 + 2 + 2
    space_a, space_b = result.flags["check_space_rules"]
    assert {c.block_id for c in space_a.citations} == {c.block_id for c in space_b.citations}
    outlet_a, outlet_b = result.flags["check_outlet_recipe"]
    assert {c.block_id for c in outlet_a.citations} == {c.block_id for c in outlet_b.citations}

    items = merge(result.lens_results("claude-sonnet-5-5"), corpus())  # no client: the code merge only
    assert len(items) == len(flags) and not any(i.source_flags for i in items)
    priced = {i.flag.check_key: price(i.flag.money_basis, None, POSTS2) for i in items}
    assert priced["check_space_rules:1:vägguttag"].point == 400  # 4 required, 3 in the calc
    assert priced["check_space_rules:2:armatur"].point == 2 * 900  # 4 required, 2 in the calc
    assert priced["check_outlet_recipe:dubbeluttag"].point == 10 * 300
    assert priced["check_outlet_recipe:ljuspunkt"].point == 10 * 200  # 2 per bedroom wanted, 1 in the calc


def test_lens_fingerprints_are_unchanged_and_a_lens_cannot_set_a_check_key() -> None:
    import hashlib

    cite = Citation(block_id="f0f0f0-000007", quote="Kabelstege i källaren ingår i elentreprenaden")
    lens = Flag(
        lens_id="regulation",
        claim_type="unpriced_requirement",
        claim="x",
        citations=[cite],
        money_basis=MoneyBasis(kind="missing_post", item="kabelstege"),
    )
    old = hashlib.sha256(b"f0f0f0-000007\nunpriced_requirement").hexdigest()
    assert fingerprint(lens) == old  # the frozen run's fingerprints still match
    assert fingerprint(lens.model_copy(update={"check_key": "check_zero_rows:x"})) != old
    two = [lens, lens.model_copy(update={"claim": "y"})]
    assert len(items_from_results([LensResult(lens_id="regulation", model="m", status="submitted", flags=two)])) == 1

    submissions = FlagSubmission("regulation", lambda items, log: ([Flag.model_validate(i) for i in items], []))
    raw = lens.model_dump(mode="json") | {"check_key": "check_zero_rows:x"}
    submissions({"flags": [raw]}, [], final=True)
    assert submissions.flags[0].check_key is None


# 2. A recipe that can't be decoded, or a check that raises, never stops the reports.


def test_a_non_utf8_recipe_is_a_recipe_error(data_root: Path) -> None:
    path = recipe_path("co-001")
    path.parent.mkdir(parents=True)
    path.write_bytes(b'source = "entered by customer"\n[rooms."k\xf6k"]\nuttag = 2\n')  # Latin-1, not UTF-8
    with pytest.raises(RecipeError, match="UTF-8"):
        load_recipe("co-001")
    assert "customer recipe unreadable" in checks_line(corpus(), CALC, "co-001")


def _granskning(error: str | None = None, lens_error: str | None = None) -> GranskningResult:
    outcome = LoopOutcome(status="error" if error else "submitted", error=error)
    lens = LensResult(lens_id="regulation", model="m", status="error" if lens_error else "submitted", error=lens_error)
    return GranskningResult(run_id="r-1", map=MapResult(None, outcome, []), lenses=[lens])


def _checks(data_root: Path, result: GranskningResult, **kw: Any) -> tuple[Any, Path, Any]:
    from granskning_fakes import ScriptedSDK

    sdk = ScriptedSDK(script)
    client = ModelClient(load_config(), sdk, Budget(5.0), run_id="r-1", company_id="co-001")
    store = RunStore("co-001")
    run_dir = store.run_dir("r-1")
    c = corpus()
    checks = pipeline._checks(
        client, store, c, CALC, Toolbox(c, CALC), result, run_dir, "granskning.json", 1, lambda _m: None, **kw
    )
    return checks, run_dir, sdk


def test_an_undecodable_recipe_still_runs_the_checks_without_it(data_root: Path) -> None:
    path = recipe_path("co-001")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe\x00 not text")
    checks, run_dir, _ = _checks(data_root, _granskning())
    assert checks.report.skipped is None and checks.report.recipe.status == "no_recipe"
    assert json.loads((run_dir / "checks.json").read_text())["recipe"]["status"] == "no_recipe"


def test_a_check_that_raises_leaves_a_note_not_a_crash(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("bug")

    monkeypatch.setattr(pipeline, "run_checks", broken)
    checks, run_dir, _ = _checks(data_root, _granskning())
    assert "RuntimeError" in (checks.report.skipped or "") and checks.lens_results("m") == []
    assert all(c.not_checked for c in checks.report.coverage)
    saved = json.loads((run_dir / "checks.json").read_text())
    assert saved["skipped"] == checks.report.skipped
    assert "Kontrollerna kördes inte" in "".join(checks_html(checks.report))


# 3. A mapped post with no quantity is unknown, not 0.


def test_a_rule_on_a_post_without_quantity_is_unknown_and_unpriced() -> None:
    rule = {**RULES_ANSWER["rules"][0], "post_id": "c0000aaaa-s1-r11"}
    result, _ = _run2(_answers(rules={"rules": [rule]}))
    (checked,) = result.report.space_rules
    assert (checked.status, checked.calc_quantity, checked.gap, checked.required) == ("unknown", None, None, 4)
    assert result.flags["check_space_rules"] == []
    assert "inget antal" in "".join(checks_html(result.report))


# 4. Room totals keep every apartment type; the apartment type is required.


def _rooms(*rooms: dict[str, Any]) -> dict[str, Any]:
    return {**RECIPE_ANSWER, "rooms": list(rooms)}


ROOM = RECIPE_ANSWER["rooms"][0]


def test_room_totals_sum_apartment_types_and_count_an_exact_repeat_once() -> None:
    other = {**ROOM, "apartment_type": "3 rok", "apartments": 10, "per_apartment": 1}  # another type, same numbers
    result, _ = _run2(_answers(recipe=_rooms(ROOM, other, dict(ROOM))))
    assert result.report.recipe.rooms["sovrum"] == 20


def test_a_room_count_without_apartment_type_gets_a_repair_and_is_dropped() -> None:
    blank = {**ROOM, "apartment_type": ""}
    result, sdk = _run2(_answers(recipe=_rooms(blank, RECIPE_ANSWER["rooms"][1])))
    calls = [p for p in sdk.sent if "outlet recipe per room type" in first_text(p)]
    assert len(calls) == 2 and "give the apartment type" in calls[1]["messages"][-1]["content"][0]["text"]
    assert "sovrum" not in result.report.recipe.rooms and result.report.recipe.rooms["kök"] == 10


# 5. A recipe cell with no mapped post is "ej kopplad", unpriced.


def test_an_unmapped_recipe_cell_is_unknown_and_unpriced() -> None:
    recipe = Recipe("entered by customer", {"sovrum": {"dubbeluttag": 3, "tv": 1}})
    result, _ = _run2(_answers(), recipe)
    rows = {(r.room_type, r.item_kind): r for r in result.report.recipe.rows}
    tv = rows[("sovrum", "tv")]
    assert tv.unmapped and (tv.calc_count, tv.gap_per_room, tv.gap_quantity) == (None, None, None)
    assert not any(f.check_key == "check_outlet_recipe:tv" for f in result.flags["check_outlet_recipe"])
    assert "ej kopplad" in "".join(checks_html(result.report))


# 6. A trigger whose quote fails after the repair is "trigger_unverified", not "no trigger".


def test_a_trigger_quote_that_never_verifies_is_its_own_status() -> None:
    answer = {**RECIPE_ANSWER, "trigger": {**RECIPE_ANSWER["trigger"], "quote": "en mening som inte står där"}}
    result, sdk = _run2(_answers(recipe=answer))
    calls = [p for p in sdk.sent if "outlet recipe per room type" in first_text(p)]
    assert len(calls) == 2  # one repair
    assert result.report.recipe.status == "trigger_unverified" and result.flags["check_outlet_recipe"] == []
    cov = next(c for c in result.report.coverage if c.check == "check_outlet_recipe")
    assert "hittades, men dess citat kunde inte verifieras" in cov.checked
    assert "hittades, men dess citat kunde inte verifieras" in "".join(checks_html(result.report))


# 7. The rooms per apartment must be in a quote, or the row stays unpriced.


def test_rooms_per_apartment_without_a_quote_leave_the_row_unpriced() -> None:
    unshown = {**ROOM, "quote": "10 lägenheter"}  # nothing shows one bedroom per apartment
    answer = _rooms(unshown, RECIPE_ANSWER["rooms"][1])
    per_room = {"post_id": "c0000aaaa-s1-r12", "room_type": "sovrum", "item_kind": "dubbeluttag", "per": "room"}
    answer["mapping"] = [per_room, RECIPE_ANSWER["mapping"][1]]
    result, sdk = _run2(_answers(recipe=answer))
    calls = [p for p in sdk.sent if "outlet recipe per room type" in first_text(p)]
    assert "rooms per apartment aren't in the quote" in calls[-1]["messages"][-1]["content"][0]["text"]
    section = result.report.recipe
    assert section.rooms_unverified == ["sovrum"] and "sovrum" not in section.rooms
    bedroom = next(r for r in section.rows if r.room_type == "sovrum")
    assert (bedroom.calc_count, bedroom.gap_per_room, bedroom.gap_quantity) == (2, 1, None)
    (flag,) = result.flags["check_outlet_recipe"]
    assert flag.money_basis.quantity == 45  # the kitchens only; the bedrooms are claimed but not priced
    assert "antal rum inte styrkt" in flag.claim


def test_rooms_per_apartment_from_a_second_cited_block_count() -> None:
    second = copy.deepcopy(ROOM) | {
        "quote": "10 lägenheter",
        "per_apartment_block_id": "f0f0f0-000003",
        "per_apartment_quote": "med ett sovrum och ett kök vardera",
    }
    result, sdk = _run2(_answers(recipe=_rooms(second, RECIPE_ANSWER["rooms"][1])))
    assert len([p for p in sdk.sent if "outlet recipe per room type" in first_text(p)]) == 1
    assert result.report.recipe.rooms["sovrum"] == 10 and result.report.recipe.rooms_unverified == []


# Nits: VAT notes aren't exclusions; the stop list; skipping the checks.


def test_exkl_moms_is_not_an_exclusion() -> None:
    def post(text: str) -> CalcPost:
        return CalcPost(id="c0000aaaa-s9-r1", sheet="K", row=1, text=text, quantity=2, amount=100)

    for vat in ("Uttag, pris exkl. moms", "Uttag exklusive moms", "Uttag exkl moms", "Uttag exkl. VAT"):
        assert zero_reason(post(vat)) is None, vat
    assert zero_reason(post("Jordning exkl.")) == "texten säger att det inte ingår"
    assert zero_reason(post("Uttag exkl. montage")) == "texten säger att det inte ingår"


def test_the_stop_list_comes_from_merge_stop_errors() -> None:
    assert {"PrefixDrift", "BudgetExceeded", "RunStopped", "ReplayMiss", "spend_cap"} <= STOP_NAMES


def test_checks_skip_after_a_stop_error_or_with_only_selected_lenses(data_root: Path) -> None:
    for result, reason in [
        (_granskning(lens_error="BudgetExceeded"), "BudgetExceeded"),
        (_granskning(error="PrefixDrift"), "PrefixDrift"),
    ]:
        checks, run_dir, sdk = _checks(data_root, result)
        assert sdk.sent == [] and reason in (checks.report.skipped or "")
        assert json.loads((run_dir / "checks.json").read_text())["skipped"] == checks.report.skipped
    checks, _, sdk = _checks(data_root, _granskning(), enabled=False)
    assert sdk.sent == [] and "--checks" in (checks.report.skipped or "")
    checks, _, sdk = _checks(data_root, _granskning(lens_error="refusal"))  # a lens's own failure doesn't skip them
    assert sdk.sent and checks.report.skipped is None


def test_run_passes_checks_only_without_lens_or_with_checks() -> None:
    import argparse

    from anbud.granskning.command import add_arguments, checks_wanted

    parser = argparse.ArgumentParser()
    add_arguments(parser)
    for argv, wanted in [([], True), (["--lens", "regulation"], False), (["--lens", "regulation", "--checks"], True)]:
        assert checks_wanted(parser.parse_args(argv)) is wanted
