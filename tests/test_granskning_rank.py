"""Rank, cap and coverage."""

from __future__ import annotations

from dataclasses import replace

from _granskning_synth import CALC_POSTS, basis, catalog, corpus, flag, lens_result, search

from anbud.granskning.contracts import ToolCall
from anbud.granskning.merge import items_from_results
from anbud.granskning.rank import NOT_ALL_CLEAR, coverage, lens_coverage, price_items, rank, score, summary
from anbud.granskning.settings import load_settings
from anbud.models import Verdict

S = load_settings()
POSTS = {p.id: p for p in CALC_POSTS}


def _verdict(item, **kw) -> Verdict:
    fields = dict(verdict="confirmed", electrical_scope=True, covered_by_calc=False, would_be_wrong_if="x")
    return Verdict(fingerprint=item.fingerprint, **{**fields, **kw})


def _items(n: int, generic: bool = False, hours: float = 100):
    flags = [
        flag(f"l{i}", f"Påstående {i}.", claim_type=f"t{i}", money_basis=basis(kind="hours_gap", hours=hours + i))
        for i in range(n)
    ]
    return price_items(items_from_results([lens_result("l", flags, generic=generic)]), catalog(), POSTS, S)


def test_the_score_multiplies_kronor_verdict_and_action_rate() -> None:
    (item,) = _items(1)
    high = item.kronor.high
    assert score(item, S) == high * S.weight(None)
    confirmed = item.model_copy(update={"verdict": _verdict(item)})
    assert score(confirmed, S) == high
    assert score(confirmed, S, {item.lenses[0]: 0.5}) == high * 0.5
    other_trade = item.model_copy(update={"verdict": _verdict(item, electrical_scope=False)})
    assert score(other_trade, S) == high * S.other_trade


def test_at_most_15_are_shown_and_generic_flags_are_separate() -> None:
    items = _items(20) + _items(3, generic=True, hours=500)
    ranked = rank(items, S)
    assert len(ranked.shown) == 15 and not any(i.generic for i in ranked.shown)
    assert len(ranked.generic) == 3
    assert {i.placement for i in ranked.logged} == {"capped"} and len(ranked.logged) == 5
    scores = [i.score for i in ranked.shown]
    assert scores == sorted(scores, reverse=True)
    small = rank(items, replace(S, max_generic=1))
    assert len(small.generic) == 1 and len(small.logged) == 7
    assert summary(ranked)["placements"] == {"shown": 15, "generic": 3, "capped": 5}


def test_refuted_covered_and_minor_flags_are_logged_with_why() -> None:
    big, other, covered = _items(3)
    (minor,) = _items(1, hours=1)
    refuted = big.model_copy(update={"verdict": _verdict(big, verdict="refuted")})
    covered = covered.model_copy(update={"verdict": _verdict(covered, covered_by_calc=True, calc_post="calc-1")})
    ranked = rank([refuted, other, covered, minor], S)
    assert [i.fingerprint for i in ranked.shown] == [other.fingerprint]
    assert {i.placement for i in ranked.logged} == {"refuted", "covered", "minor"}


def test_a_corrected_money_basis_reprices_the_flag() -> None:
    (item,) = _items(1)
    corrected = _verdict(item, verdict="partly", corrected_money_basis=basis(kind="hours_gap", hours=10))
    (repriced,) = price_items([item.model_copy(update={"verdict": corrected})], catalog(), POSTS, S)
    assert repriced.kronor.point == 10 * 500 and repriced.minor


def test_coverage_comes_from_the_tool_log_only() -> None:
    log = [
        search("byggström"),
        ToolCall(role="lens", turn=1, tool="read_blocks", input={"ids": []}, result_ids=["aaa111-000004", "nope"]),
        ToolCall(role="lens", turn=2, tool="read_blocks", input={"ids": []}, result_ids=["bbb222-000001"]),
        ToolCall(role="lens", turn=2, tool="calc_posts", input={}),
        ToolCall(
            role="lens", turn=3, tool="view_tile", input={"sheet": "s", "tile": "tile-1"}, refused="not available"
        ),
        ToolCall(role="lens", turn=3, tool="read_blocks", input={"ids": ["x"]}, error="no such block"),
    ]
    result = lens_result("trade_boundaries", [], tool_log=log, status="truncated", turns=8)
    cov = lens_coverage(result, corpus())
    assert cov.documents_opened == ["Beskrivning.pdf", "Mangder.xlsx"]
    assert cov.blocks_read == 2 and cov.searches == ["byggström"] and cov.sheets_opened == ["Mangder.xlsx / Mängder"]
    assert cov.tiles_viewed == 0 and cov.calc_calls == 1 and cov.refused_calls == 1 and cov.failed_calls == 1
    assert any("gräns" in line for line in cov.not_checked)
    assert any("2 av 4 dokument öppnades inte: Ritning.pdf, Underlag.dwg" in line for line in cov.not_checked)
    assert any("Ritning.pdf" in line and "inte tittade" in line for line in cov.not_checked)


def test_coverage_never_says_all_clear() -> None:
    log = [
        search("x"),
        ToolCall(role="lens", turn=1, tool="read_blocks", input={}, result_ids=["aaa111-000001", "bbb222-000001"]),
        ToolCall(role="lens", turn=1, tool="view_tile", input={"sheet": "s", "tile": "tile-1"}, result_ids=["tile-1"]),
    ]
    tender = coverage([lens_result("a", [], tool_log=log)], corpus(), ["Bilaga 7"])
    assert tender.note == NOT_ALL_CLEAR and tender.documents == 4
    assert tender.not_read_by_prep == ["Underlag.dwg (unsupported)"] and tender.named_not_present == ["Bilaga 7"]
    assert tender.lenses[0].not_checked == ["1 av 4 dokument öppnades inte: Underlag.dwg"]
    text = tender.model_dump_json().lower()
    assert "all clear" not in text and "inga fel" not in text
