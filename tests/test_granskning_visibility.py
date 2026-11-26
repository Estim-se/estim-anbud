"""Real catches stay visible: unpriced flags get their own list, and the XLSX lists every flag not refuted."""

from __future__ import annotations

from dataclasses import replace

from _granskning_synth import basis, catalog, corpus, flag, lens_result
from openpyxl import Workbook

from anbud.granskning.items import Item
from anbud.granskning.merge import items_from_results
from anbud.granskning.rank import coverage, price_items, rank
from anbud.granskning.report import build_report, build_workbook, column_keys, render_html
from anbud.granskning.settings import load_settings
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
