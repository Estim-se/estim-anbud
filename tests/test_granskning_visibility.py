"""Real catches stay visible: unpriced flags get their own list, and the XLSX lists every flag not refuted."""

from __future__ import annotations

from dataclasses import replace

from _granskning_synth import basis, catalog, corpus, flag, lens_result
from openpyxl import Workbook

from anbud import tools
from anbud.granskning.contracts import ToolCall
from anbud.granskning.items import Item
from anbud.granskning.kronor import price
from anbud.granskning.lenses import load_lenses
from anbud.granskning.merge import items_from_results
from anbud.granskning.prefix import brief_text
from anbud.granskning.rank import coverage, lens_coverage, price_items, rank, unpriced_queue
from anbud.granskning.report import build_report, build_workbook, column_keys, render_html
from anbud.granskning.settings import load_settings
from anbud.granskning.toolbox import Toolbox
from anbud.granskning.verify import select
from anbud.models import Verdict

S = load_settings()
NOWHERE = basis(item="Posten finns inte i katalogen")


def _verdict(item: Item, verdict: str, **kw: object) -> Item:
    fields = {"electrical_scope": True, "covered_by_calc": False, "would_be_wrong_if": "x", **kw}
    return item.model_copy(update={"verdict": Verdict(fingerprint=item.fingerprint, verdict=verdict, **fields)})


def _items() -> dict[str, Item]:
    """20 priced flags, big enough to fill the priced list, and unpriced ones with every verifier outcome."""
    hours = basis(kind="hours_gap", hours=500)
    priced = [flag("contract", f"Stor post {n}.", claim_type=f"p{n:02}", money_basis=hours) for n in range(20)]
    unpriced = {
        "unverified": flag("contract", "Overifierat krav.", claim_type="u1", money_basis=NOWHERE),
        "partly": flag("contract", "Delvis krav.", claim_type="u2", money_basis=NOWHERE),
        "confirmed": flag("contract", "Bekräftat krav.", claim_type="u3", money_basis=NOWHERE),
        "exposed": flag("contract", "Avtalsrisk.", claim_type="u4", money_basis=NOWHERE, contractual_exposure=True),
        "refuted": flag("contract", "Motbevisat.", claim_type="u5", money_basis=NOWHERE),
        "covered": flag("contract", "Redan i kalkylen.", claim_type="u6", money_basis=NOWHERE),
    }
    generic = flag("generic_scope", "Generiskt utan pris.", claim_type="g1", money_basis=NOWHERE)
    results = [
        lens_result("contract", [*priced, *unpriced.values()]),
        lens_result("generic_scope", [generic], generic=True),
    ]
    items = {i.flag.claim_type: i for i in price_items(items_from_results(results), catalog(), {}, S)}
    items["u2"] = _verdict(items["u2"], "partly")
    items["u3"] = _verdict(items["u3"], "confirmed")
    items["u5"] = _verdict(items["u5"], "refuted")
    items["u6"] = _verdict(items["u6"], "confirmed", covered_by_calc=True)
    return items


def _placements(ranked_items: list[Item]) -> dict[str, str | None]:
    return {i.flag.claim_type: i.placement for i in ranked_items}


def test_unpriced_flags_get_their_own_list_and_priced_flags_cant_push_them_out() -> None:
    ranked = rank(list(_items().values()), S)
    assert len(ranked.shown) == S.max_shown and all(i.kronor and i.kronor.priced for i in ranked.shown)
    assert [i.flag.claim_type for i in ranked.unpriced] == ["u3", "u2", "u4", "u1"]  # confirmed, partly, unverified
    every = _placements(ranked.shown + ranked.unpriced + ranked.generic + ranked.logged)
    assert {every[t] for t in ("u1", "u2", "u3", "u4")} == {"shown_unpriced"}
    assert every["u5"] == "refuted" and every["u6"] == "covered" and every["g1"] == "generic"
    assert sum(p == "capped" for p in every.values()) == 20 - S.max_shown  # only priced overflow


def test_severity_breaks_ties_within_an_outcome_and_the_list_has_its_own_cap() -> None:
    ranked = rank(list(_items().values()), replace(S, max_unpriced_shown=2))
    assert [i.flag.claim_type for i in ranked.unpriced] == ["u3", "u2"]
    assert _placements(ranked.logged)["u4"] == "capped" and _placements(ranked.logged)["u1"] == "capped"
    unverified = rank([_items()["u1"], _items()["u4"]], S)
    assert [i.flag.claim_type for i in unverified.unpriced] == ["u4", "u1"]  # contractual exposure first


def test_the_flat_unpriced_weight_plays_no_part_in_the_unpriced_list() -> None:
    items = list(_items().values())
    for unpriced_sek in (0.0, 1.0, 20_000.0, 10_000_000.0):
        ranked = rank(items, replace(S, unpriced_sek=unpriced_sek))
        assert [i.flag.claim_type for i in ranked.unpriced] == ["u3", "u2", "u4", "u1"], unpriced_sek
        assert all(i.kronor and i.kronor.priced for i in ranked.shown)


def _report(max_shown: int = 15):
    settings = replace(S, max_shown=max_shown)
    return build_report(
        run_id="run-1",
        company_id="co-001",
        tender_id="t-0001",
        ranked=rank(list(_items().values()), settings),
        coverage=coverage([], corpus()),
        corpus=corpus(),
        catalog=catalog(),
        materiality_sek=S.materiality_sek,
        max_shown=settings.max_shown,
        max_unpriced_shown=settings.max_unpriced_shown,
    )


def _rows(wb: Workbook, sheet: str, key: str) -> list[str]:
    col = column_keys(wb, sheet)[key]
    return [wb[sheet].cell(row=r, column=col).value for r in range(2, wb[sheet].max_row + 1)]


def test_both_reports_show_the_unpriced_list_and_nothing_not_refuted_is_hidden() -> None:
    report = _report()
    page = render_html(report)
    assert "Krav utan prisuppgift (4)" in page and "Bekräftat krav." in page
    outside = 20 - 15 + 1  # the priced overflow and the covered flag
    assert f"{outside} flaggor som inte motbevisats ryms inte i listorna ovan" in page and "Alla flaggor" in page
    wb = build_workbook(report)
    assert len(_rows(wb, "Krav utan prisuppgift", "claim")) == 4
    every = dict(zip(_rows(wb, "Alla flaggor", "claim"), _rows(wb, "Alla flaggor", "placement"), strict=True))
    assert len(every) == 27 - 1 and "Motbevisat." not in every  # all 27 flags but the refuted one
    assert every["Bekräftat krav."] == "Visas, utan prisuppgift" and every["Stor post 19."] != "Visas"
    assert every["Generiskt utan pris."] == "Generisk" and every["Redan i kalkylen."] == "Täcks redan av kalkylen"


def test_the_html_count_is_only_what_is_outside_the_lists() -> None:
    page = render_html(_report(max_shown=30))
    assert "1 flaggor som inte motbevisats ryms inte i listorna ovan" in page  # only the covered flag


def test_catalog_search_returns_ids_names_units_and_categories_never_prices() -> None:
    box = Toolbox(corpus(), [], catalog=catalog())
    out = box.run("catalog_search", {"query": "armatur trapphus"})
    assert out.error is None and out.result_ids == ["i1"]
    assert "id=i1 | Armatur trapphus | unit=st | category=6" in out.content
    assert "1000" not in out.content and "500" not in out.content and "hours" not in out.content
    fallback = box.run("catalog_search", {"query": "uttag vägguttag"})  # not every word matches: any word does
    assert fallback.result_ids == ["i2"]
    assert "stays unpriced" in box.run("catalog_search", {"query": "rymdhiss"}).content
    assert Toolbox(corpus(), []).run("catalog_search", {"query": "armatur"}).error == "no_catalog"
    assert box.run("catalog_search", {"query": ""}).error == "invalid_input"


def test_every_lens_may_search_the_catalog_and_the_verifier_may_not() -> None:
    assert all("catalog_search" in lens.tools for lens in load_lenses())
    assert (
        tools.refusal("lens", "catalog_search") is None
        and tools.refusal("lens_generic_scope", "catalog_search") is None
    )
    assert tools.refusal("verifier", "catalog_search") is not None
    assert "catalog_search" in brief_text() and "catalog_id" in brief_text()


def test_kronor_prices_a_missing_post_by_catalog_id_first() -> None:
    by_id = price(basis(item="Belysning i trapphusen", catalog_id="i1", quantity=2), catalog(), {}, S)
    assert by_id.priced and by_id.point == 2 * (1.0 * 500 + 1000) and "Armatur trapphus" in by_id.label
    gap = price(basis(kind="quantity_gap", catalog_id="i2", quantity=10), catalog(), {}, S)
    assert gap.priced and gap.point == 10 * (0.5 * 500 + 100)
    stale = price(basis(item="Vägguttag", catalog_id="no-such-id", quantity=1), catalog(), {}, S)
    assert stale.priced and "Vägguttag" in stale.label  # an unknown id falls back to the name
    nothing = price(basis(item="Rymdhiss", catalog_id="no-such-id"), catalog(), {}, S)
    assert not nothing.priced and nothing.reason == "posten finns inte i katalogen"


def test_a_flag_nothing_in_the_catalog_fits_stays_unpriced_and_is_shown() -> None:
    f = flag("contract", "Kravet saknar katalogpost.", claim_type="u9", money_basis=basis(item="Rymdhiss"))
    (item,) = price_items(items_from_results([lens_result("contract", [f])]), catalog(), {}, S)
    ranked = rank([item], S)
    assert [i.placement for i in ranked.unpriced] == ["shown_unpriced"]


def test_catalog_searches_count_in_coverage() -> None:
    log = [ToolCall(role="lens", turn=1, tool="catalog_search", input={"query": "armatur"}, result_ids=["i1"])]
    assert lens_coverage(lens_result("contract", [], tool_log=log), corpus()).catalog_searches == 1


def _pool(priced: int, unpriced: int) -> list[Item]:
    hours = basis(kind="hours_gap", hours=500)
    flags = [flag("contract", f"Pris {n}.", claim_type=f"p{n:02}", money_basis=hours) for n in range(priced)]
    flags += [
        flag("contract", f"Krav {n}.", claim_type=f"u{n:02}", money_basis=NOWHERE, contractual_exposure=n == 3)
        for n in range(unpriced)
    ]
    return price_items(items_from_results([lens_result("contract", flags)]), catalog(), {}, S)


def _select(items: list[Item], slots: int) -> list[tuple[Item, str]]:
    kronor = {i.fingerprint: (i.kronor.high if i.kronor and i.kronor.priced else S.unpriced_sek) for i in items}
    return select(
        items, lambda i: kronor[i.fingerprint], 25, 3, "run-1", reserved=unpriced_queue(items, S), reserved_slots=slots
    )


def test_the_verifier_reserves_slots_for_unpriced_flags_without_calling_more_often() -> None:
    assert S.verifier_unpriced_slots == 7 and (S.verifier_top_n, S.verifier_sampled) == (25, 3)
    items = _pool(priced=40, unpriced=12)
    chosen = _select(items, 7)
    assert len(chosen) == 25 + 3
    top = [i for i, how in chosen if how == "verified"]
    unpriced_top = [i.flag.claim_type for i in top if not i.kronor.priced]
    assert len(top) == 25 and len(unpriced_top) == 7
    assert unpriced_top[0] == "u03"  # the queue's order: contractual exposure first
    assert sum(1 for i in top if i.kronor.priced) == 18  # the rest by kronor, as before
    assert len({i.fingerprint for i, _ in chosen}) == 28  # nothing verified twice


def test_slots_the_unpriced_flags_cant_fill_go_back_to_the_priced_side() -> None:
    chosen = _select(_pool(priced=40, unpriced=2), 7)
    top = [i for i, how in chosen if how == "verified"]
    assert len(chosen) == 28 and len(top) == 25
    assert sum(1 for i in top if not i.kronor.priced) == 2 and sum(1 for i in top if i.kronor.priced) == 23
    none = _select(_pool(priced=40, unpriced=0), 7)
    assert sum(1 for i, how in none if how == "verified") == 25


def test_the_total_never_exceeds_top_n_plus_sampled() -> None:
    for priced in (0, 3, 25, 60):
        for unpriced in (0, 5, 30):
            for slots in (0, 7, 25, 40):
                items = _pool(priced, unpriced)
                chosen = _select(items, slots)
                assert len(chosen) == min(len(items), 28), (priced, unpriced, slots)
                assert sum(1 for _, how in chosen if how == "verified") == min(len(items), 25)
