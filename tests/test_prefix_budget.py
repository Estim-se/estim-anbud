"""The prefix budget on a generated 1000-page tender: priority 1 stays in full and under the budget, documents beyond
it become search-only in a fixed order with their outline kept, and a priority 1 that can't fit stops the run before
any model call. Synthetic documents only."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from granskning_fakes import DOC_AF, DOC_EL, DOC_RIT, ScriptedSDK, first_text, png_bytes, reply, write_prep
from large_tender import DOCS, EL_LINE, TOTAL_PAGES, doc_id, write_large_prep
from openpyxl import load_workbook
from test_granskning_pipeline import CLEAN, _calc, _catalog
from test_granskning_pipeline import script as pipeline_script
from test_runguard import ledger_with

from anbud import cli, model_client
from anbud import config as config_module
from anbud.evals import split_heldout as sh
from anbud.granskning import provenance
from anbud.granskning.budget import (
    DOC_CHARS,
    SEARCH_ONLY_CORE,
    SEARCH_ONLY_P1,
    PrefixBudget,
    PrefixPlan,
    _card_tokens_each,
    apply_plan,
    core_search_only,
    fit_prefix,
    p1_search_only,
    plan_prefix,
    priorities,
)
from anbud.granskning.calc import load_calc
from anbud.granskning.corpus import Corpus
from anbud.granskning.page_cards import card_from_message
from anbud.granskning.pipeline import add_cards
from anbud.granskning.prefix import SEARCH_ONLY_NOTE, corpus_text, estimate_tokens, system_blocks
from anbud.granskning.rank import coverage
from anbud.granskning.toolbox import Toolbox
from anbud.selection import load_selected
from anbud.store import RunStore

BUDGET = PrefixBudget(450_000, 0.10)
NAMES = {doc_id(n): path.rsplit("/", 1)[-1] for n, (path, _, _) in enumerate(DOCS, 1)}
BY_NAME = {name: d for d, name in NAMES.items()}


@pytest.fixture(scope="module")
def large(tmp_path_factory: pytest.TempPathFactory) -> tuple[Corpus, Corpus, float]:
    started = time.monotonic()
    prep = write_large_prep(tmp_path_factory.mktemp("large") / "t" / "prep")
    raw = Corpus.load(prep)
    selected, _ = load_selected(prep)
    return raw, selected, time.monotonic() - started


def test_on_a_1000_page_tender_priority_1_fits_and_the_prefix_stays_under_the_budget(large) -> None:
    raw, selected, seconds = large
    assert TOTAL_PAGES == 1000 and estimate_tokens(system_blocks(raw, [])) > 1_400_000  # a full tender set
    plan = plan_prefix(selected, [], BUDGET)
    assert plan.p1_fits and plan.p0 + plan.p1 < 0.5 * BUDGET.tokens  # P1 is tight: far from the budget
    p1 = sorted(NAMES[d] for d, level in priorities(selected).items() if level == 1)
    assert p1 == sorted(
        ["AF.pdf", "Kontraktsförslag.pdf", "Rambeskrivning.pdf", "Tidplan.pdf", "E-63-1-001 Teknisk beskrivning el.pdf"]
    )
    applied = apply_plan(selected, plan)
    rendered = estimate_tokens(system_blocks(applied, []))
    assert rendered <= plan.total <= BUDGET.tokens  # the plan errs high, never low
    t0 = time.monotonic()
    plan_prefix(selected, [], BUDGET)
    assert seconds + (time.monotonic() - t0) < 30


def test_documents_beyond_the_budget_become_search_only_priority_3_first_largest_first(large) -> None:
    _, selected, _ = large
    plan = plan_prefix(selected, [], BUDGET)
    annexes = [BY_NAME[f"Bilaga {n}.pdf"] for n in range(1, 6)]  # priority 3, equal sizes: by doc id
    assert plan.search_only == [*annexes, BY_NAME["Rumsbeskrivning.pdf"]]  # then the largest priority 2 document
    assert not any(priorities(selected)[d] == 1 for d in plan.search_only)


def test_a_search_only_document_keeps_its_outline_and_stays_readable(large) -> None:
    _, selected, _ = large
    applied = apply_plan(selected, plan_prefix(selected, [], BUDGET))
    text = corpus_text(applied)
    annex = BY_NAME["Bilaga 1.pdf"]
    heading, paragraph = f"{annex[:6]}-000001", f"{annex[:6]}-000002"
    assert 'search_only="prefixbudget"' in text and SEARCH_ONLY_NOTE in text
    assert f"{heading} p1 H: Avsnitt 1" in text and paragraph not in text  # outline in, text out
    out = Toolbox(applied, []).run("read_blocks", {"ids": [paragraph]})
    assert paragraph in out.content and out.error is None  # read_blocks still reaches it
    cov = coverage([], applied)
    assert "Bilaga 1.pdf (10 s.)" in cov.search_only_documents and len(cov.search_only_documents) == 6


def test_other_trades_lines_past_their_cap_leave_the_prefix_from_the_last(large) -> None:
    _, selected, _ = large
    boundary = [b for b in selected.order if selected.blocks[b].doc_id in selected.excluded]
    lines = [b for b in boundary if selected.blocks[b].kind != "heading"]
    assert len(lines) == 10 and all(selected.blocks[b].text == EL_LINE for b in lines)
    plan = plan_prefix(selected, [], PrefixBudget(BUDGET.tokens, 0.0006))  # a cap of 270 tokens
    assert plan.boundary_dropped and plan.boundary_dropped == boundary[len(boundary) - len(plan.boundary_dropped) :]
    text = corpus_text(apply_plan(selected, plan))
    assert boundary[0] in text and plan.boundary_dropped[-1] not in text


@pytest.fixture
def tender(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(provenance, "git_state", lambda root=None: CLEAN)
    ledger_with(data_root, t_0001=sh.DEV)
    blob = RunStore("co-001").put_blob(png_bytes())
    base = data_root / "co-001" / "dev" / "t-0001"
    write_prep(base / "prep", tile_blob=blob)
    _calc(base / "calc")
    _catalog(data_root)
    return base


def _budget(monkeypatch: pytest.MonkeyPatch, tokens: int) -> None:
    real = config_module.load_config

    def load_config(*args, **kwargs):
        config = real(*args, **kwargs)
        config.budgets["prefix"] = {"budget_tokens": tokens, "boundary_share": 0.1}
        return config

    monkeypatch.setattr(config_module, "load_config", load_config)


def test_when_not_even_the_outlines_fit_the_run_stops_before_any_model_call(
    tender: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The last rung: P0 alone is over the budget. Mutation: `.stops:` -> `.stops and False:` on the check before the
    cards in `run_pipeline`, and the tile's card is paid for before `fit_prefix` stops the run."""
    _budget(monkeypatch, 1000)  # less than the brief alone
    sdk = ScriptedSDK(lambda params: pytest.fail("no model call may be made"))
    monkeypatch.setattr(model_client, "keychain_key", lambda **_: "sk-test")
    monkeypatch.setattr(model_client, "make_sdk_client", lambda config, key: sdk)
    assert cli.main(["run", "co-001", "t-0001", "--run-id", "t-0001-budget"]) != 0
    err = capsys.readouterr().err
    assert "stopped: the prefix's fixed part" in err and "every document's outline" in err
    assert "budget of 1000 tokens" in err
    assert sdk.sent == []  # not even a page card


def test_the_dry_run_shows_priority_1_on_its_own(
    tender: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["run", "co-001", "t-0001", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "prefix budget 450000 tokens: fixed " in out and "priority 1 fits (" in out
    _budget(monkeypatch, 1000)
    assert cli.main(["run", "co-001", "t-0001", "--dry-run"]) == 0
    assert "nothing fits: a paid run stops before any model call" in capsys.readouterr().out


# An invented card: a dense page of text, larger than the room left in the budget.
BIG_CARD = "\n\n".join(f"Påhittat stycke {n}: " + "text på en inskannad sida " * 20 for n in range(12))


def _tile_on(prep: Path, doc: str, page: int, name: str | None = None) -> None:
    """Moves the synthetic tender's one tile to `doc`, and renames that document when `name` is given."""
    tiles = json.loads((prep / "tiles.json").read_text())
    tiles[0] |= {"doc_id": doc, "page": page}
    (prep / "tiles.json").write_text(json.dumps(tiles))
    if name is not None:
        docs = json.loads((prep / "documents.json").read_text())
        for d in docs:
            if d["doc_id"] == doc:
                d["paths"] = [f"inbox/{name}"]
        (prep / "documents.json").write_text(json.dumps(docs))


def test_a_card_never_moves_the_floor_so_a_run_never_stops_after_paying_for_one(tender: Path) -> None:
    """A dense card on a scanned AF page takes the core over the budget once it is read. The floor, every document's
    outline, is the same with the card, so the check before the cards holds: the run reads the card and demotes the
    AF instead of stopping. Mutation: count each document's full text in the floor (`min(sizes[d], ...)` ->
    `sizes[d]`), and the card moves it."""
    _tile_on(tender / "prep", DOC_AF, 2)  # a scanned page of the AF: its card's text joins the core
    corpus, _ = load_selected(tender / "prep")
    calc = load_calc(tender / "calc")
    without = plan_prefix(corpus, calc, BUDGET)
    tokens = without.p0 + without.p1 + 1000
    before = plan_prefix(corpus, calc, PrefixBudget(tokens, 0.1), corpus.tiles.values())
    assert not before.stops and not before.core_fits  # the core's cards could take it over: no stop for that
    card = card_from_message(next(iter(corpus.tiles.values())), reply(BIG_CARD))
    carded = add_cards(corpus, card)
    after = plan_prefix(carded, calc, PrefixBudget(tokens, 0.1))
    assert after.floor == before.floor and not after.stops
    assert after.core_search_only == [DOC_AF] and after.total <= tokens
    fit_prefix(carded, calc, {"prefix": {"budget_tokens": tokens, "boundary_share": 0.1}})  # no stop after the card


def test_a_document_whose_only_text_comes_from_its_cards_is_in_the_floor_before_they_are_read(tmp_path: Path) -> None:
    """The synthetic drawing has a tile and no block: before its card it has no outline yet, after it its <doc> lines
    are in the prefix. Mutation: delete the `least |= ...` line for `pending` documents in `plan_prefix`, and the
    card moves the floor."""
    prep = write_prep(tmp_path / "prep")
    corpus, _ = load_selected(prep)
    tile = next(iter(corpus.tiles.values()))
    assert tile.doc_id == DOC_RIT and not any(corpus.blocks[b].doc_id == DOC_RIT for b in corpus.order)
    before = plan_prefix(corpus, [], BUDGET, corpus.tiles.values())
    after = plan_prefix(add_cards(corpus, card_from_message(tile, reply(BIG_CARD))), [], BUDGET)
    assert before.floor == after.floor


def test_the_card_bound_counts_only_cards_that_can_reach_priority_1(tmp_path: Path) -> None:
    prep = write_prep(tmp_path / "drawing" / "prep")
    _tile_on(prep, DOC_RIT, 1, "E-63-1-001 Elplan.pdf")  # a numbered electrical drawing: priority 2, never priority 1
    corpus, _ = load_selected(prep)
    assert priorities(corpus)[DOC_RIT] == 2
    assert plan_prefix(corpus, [], BUDGET, corpus.tiles.values()).cards == 0

    prep = write_prep(tmp_path / "scan" / "prep")
    _tile_on(prep, DOC_RIT, 1, "Underlag 7.pdf")  # a scan under a plain name: its card may classify it as the AF
    corpus, _ = load_selected(prep)
    tile = next(iter(corpus.tiles.values()))
    plan = plan_prefix(corpus, [], BUDGET, corpus.tiles.values())
    assert priorities(corpus)[DOC_RIT] == 3 and plan.cards == _card_tokens_each(tile) + int(DOC_CHARS / 1.6)
    assert plan.summary()["p1_card_tokens_max"] == plan.cards
    assert "p1_card_tokens_max" not in plan_prefix(corpus, [], BUDGET).summary()  # a plan after the cards: unchanged

    _tile_on(prep, DOC_AF, 2)  # a priority-1 document: one full card, its own text is counted already
    corpus, _ = load_selected(prep)
    assert plan_prefix(corpus, [], BUDGET, corpus.tiles.values()).cards == _card_tokens_each(tile)
    assert _card_tokens_each(tile) * 1.6 > len(BIG_CARD)  # the bound holds the invented dense page


# --- A priority 1 over the budget: demoted to search, except the core (the AF and the contract) -----------------------


def test_over_the_budget_priority_1_beyond_the_core_becomes_search_only_largest_first(large) -> None:
    """The first rung: P1 without its core. Mutation: `*(d for d in p1 if d not in small), *small, *core` -> `*core,
    *(d for d in p1 if d not in small), *small` in `_demotion_order`, and the AF goes before the rest of P1."""
    _, selected, _ = large
    full = plan_prefix(selected, [], BUDGET)
    budget = PrefixBudget(100_000, 0.10)  # holds the core, not the rest of priority 1
    plan = plan_prefix(selected, [], budget)
    assert not plan.p1_fits and not plan.stops and full.core == plan.core
    p1 = [NAMES[d] for d in plan.p1_search_only]
    assert p1 == ["E-63-1-001 Teknisk beskrivning el.pdf", "Rambeskrivning.pdf"]  # the largest first
    assert plan.search_only[-2:] == plan.p1_search_only  # after every priority 2 and 3 document
    kept = {NAMES[d] for d, level in priorities(selected).items() if level == 1 and d not in plan.search_only}
    assert kept == {"AF.pdf", "Kontraktsförslag.pdf", "Tidplan.pdf"}  # the core, and a small time plan
    assert plan.total <= budget.tokens
    summary = plan.summary()
    assert summary["p1_search_only"] == plan.p1_search_only and summary["stops"] is False

    applied = apply_plan(selected, plan)  # nothing is lost: every block stays readable, and the reason says priority 1
    assert p1_search_only(applied) == sorted(plan.p1_search_only, key=list(applied.search_only).index)
    assert set(applied.blocks) == set(selected.blocks) and applied.all_order == selected.all_order
    el = BY_NAME["E-63-1-001 Teknisk beskrivning el.pdf"]
    out = Toolbox(applied, []).run("read_blocks", {"ids": [f"{el[:6]}-000002"]})
    assert out.error is None and f"{el[:6]}-000002" in out.content
    assert f'search_only="{SEARCH_ONLY_P1}"' in corpus_text(applied)
    names = coverage([], applied).search_only_priority_1
    assert names == ["E-63-1-001 Teknisk beskrivning el.pdf (60 s.)", "Rambeskrivning.pdf (20 s.)"]


def test_past_the_rest_of_priority_1_the_core_goes_largest_first_and_the_floor_is_the_last_rung(large) -> None:
    """Each rung in turn on the 1000-page tender: a budget just under the last plan demotes the next document, the
    time plan before the core and the AF (the larger core document) before the contract. At the floor everything is
    search-only and the run still goes on; a token under it, it stops. Mutation: put `*core` before `*small` in
    `_demotion_order`, and the AF goes before the time plan."""
    _, selected, _ = large
    boundary = [b for b in selected.order if selected.blocks[b].doc_id in selected.excluded]
    plan = plan_prefix(selected, [], PrefixBudget(100_000, 0.10))
    while not plan.core_search_only:
        before = list(plan.search_only)
        plan = plan_prefix(selected, [], PrefixBudget(plan.total - 1, 0.10))
        assert plan.search_only[: len(before)] == before and len(plan.search_only) > len(before)
    p1 = ["E-63-1-001 Teknisk beskrivning el.pdf", "Rambeskrivning.pdf", "Tidplan.pdf", "AF.pdf"]
    assert [NAMES[d] for d in plan.p1_search_only] == p1 and [NAMES[d] for d in plan.core_search_only] == ["AF.pdf"]
    assert not plan.stops and plan.total <= plan.budget and not plan.core_fits  # the dry run's word for it
    applied = apply_plan(selected, plan)
    assert core_search_only(applied) == [BY_NAME["AF.pdf"]]
    assert f'search_only="{SEARCH_ONLY_CORE}"' in corpus_text(applied)
    assert coverage([], applied).search_only_core == ["AF.pdf (30 s.)"]

    at_floor = plan_prefix(selected, [], PrefixBudget(plan.floor, 0.10))
    assert not at_floor.stops and at_floor.total <= plan.floor
    assert set(at_floor.search_only) == set(priorities(selected)) and len(at_floor.core_search_only) == 2
    assert at_floor.boundary_dropped == boundary  # the lines within the cap go last, from the last
    under = plan_prefix(selected, [], PrefixBudget(plan.floor - 1, 0.10))
    assert under.stops and under.search_only == [] and under.total == plan.floor


def test_the_exam_counts_a_demoted_core_and_stops_only_below_the_floor(tmp_path: Path) -> None:
    """Mutation: `"core_search_only": len(plan.core_search_only)` -> `0` in `selection_sizes`."""
    from anbud.evals import exam

    prep = write_large_prep(tmp_path / "large" / "prep")
    corpus, _ = load_selected(prep)
    floor = plan_prefix(corpus, [], PrefixBudget(10**9, 0.10)).floor
    calc = tmp_path / "large" / "calc"
    sizes = exam.selection_sizes(prep, calc, {"prefix": {"budget_tokens": floor + 100}})
    assert sizes is not None and sizes["stops"] is False and sizes["core_search_only"] == 2
    sizes = exam.selection_sizes(prep, calc, {"prefix": {"budget_tokens": floor - 100}})
    assert sizes is not None and sizes["stops"] is True


def test_the_exam_counts_priority_1_read_through_search_and_no_stop(tmp_path: Path) -> None:
    from anbud.evals import exam

    prep = write_large_prep(tmp_path / "large" / "prep")
    sizes = exam.selection_sizes(prep, tmp_path / "large" / "calc", {"prefix": {"budget_tokens": 100_000}})
    assert sizes is not None and sizes["p1_fits"] is False and sizes["stops"] is False and sizes["p1_search_only"] == 2


def _rename(prep: Path, names: dict[str, str]) -> None:
    docs = json.loads((prep / "documents.json").read_text())
    for d in docs:
        if d["doc_id"] in names:
            d["paths"] = [f"inbox/{names[d['doc_id']]}"]
    (prep / "documents.json").write_text(json.dumps(docs))


TB_EL = "E-63-1-002 Teknisk beskrivning el.pdf"


def _p1_tender(tender: Path) -> PrefixPlan:
    """The synthetic tender with its electrical description as priority 1 beside the AF, and the drawing numbered."""
    _rename(tender / "prep", {DOC_EL: TB_EL, DOC_RIT: "E-63-1-001 Elplan.pdf"})
    corpus, _ = load_selected(tender / "prep")
    assert priorities(corpus) == {DOC_AF: 1, DOC_EL: 1, DOC_RIT: 2}
    return plan_prefix(corpus, load_calc(tender / "calc"), BUDGET, corpus.tiles.values())


def test_a_priority_1_over_the_budget_is_reviewed_through_search_instead_of_stopping(
    tender: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    full = _p1_tender(tender)
    _budget(monkeypatch, full.p0 + full.p1 - 1)  # the AF fits, all of priority 1 doesn't
    sdk = ScriptedSDK(pipeline_script)
    monkeypatch.setattr(model_client, "keychain_key", lambda **_: "sk-test")
    monkeypatch.setattr(model_client, "make_sdk_client", lambda config, key: sdk)
    assert cli.main(["run", "co-001", "t-0001", "--run-id", "t-0001-p1"]) == 0
    assert "documents search-only (1 of priority 1)" in capsys.readouterr().out

    record = json.loads((RunStore("co-001").run_dir("t-0001-p1") / "granskning.json").read_text())
    plan = next(v for k, v in record.items() if isinstance(v, dict) and "p1_search_only" in v)
    assert plan["p1_search_only"] == [DOC_EL] and plan["stops"] is False and plan["p1_fits"] is False
    lens_calls = [first_text(p) for p in sdk.sent if "review lens `" in str(p["messages"][0]["content"])]
    assert lens_calls and all("<search_only_priority_1>" in text and DOC_EL in text for text in lens_calls)
    report = tender / "runs" / "t-0001-p1" / "report"
    assert "Prioritet 1 granskad via sökning" in (report / "granskning.html").read_text(encoding="utf-8")
    about = {r[0].value: r[1].value for r in load_workbook(report / "granskning.xlsx")["Om rapporten"].iter_rows()}
    assert about["Prioritet 1 granskad via sökning, inte i sin helhet"] == f"{TB_EL} (3 s.)"


def test_when_the_core_alone_is_over_the_budget_it_is_reviewed_through_search_and_the_review_is_partial(
    tender: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The core's rung end to end. Mutation: `core = ""` in `lens_message`, and the lenses aren't told the AF is
    search-only."""
    full = _p1_tender(tender)
    _budget(monkeypatch, full.floor + 1)  # this small tender's outlines leave room for little more than themselves
    sdk = ScriptedSDK(pipeline_script)
    monkeypatch.setattr(model_client, "keychain_key", lambda **_: "sk-test")
    monkeypatch.setattr(model_client, "make_sdk_client", lambda config, key: sdk)
    assert cli.main(["run", "co-001", "t-0001", "--run-id", "t-0001-core"]) == 0
    assert "(2 of priority 1, 1 of them the AF or the contract)" in capsys.readouterr().out

    record = json.loads((RunStore("co-001").run_dir("t-0001-core") / "granskning.json").read_text())
    plan = next(v for k, v in record.items() if isinstance(v, dict) and "core_search_only" in v)
    assert plan["core_search_only"] == [DOC_AF] and plan["p1_search_only"] == [DOC_EL, DOC_AF]
    assert plan["stops"] is False and plan["core_search_only_documents"] == 1
    lens_calls = [first_text(p) for p in sdk.sent if "review lens `" in str(p["messages"][0]["content"])]
    assert lens_calls and all("hold the administrative terms (AF) or the contract" in t for t in lens_calls)
    assert all(DOC_AF in t.split("<search_only_priority_1>")[1] for t in lens_calls)
    report = tender / "runs" / "t-0001-core" / "report"
    page = (report / "granskning.html").read_text(encoding="utf-8")
    assert "Partiell granskning: AF eller kontrakt via sökning" in page
    assert "AF och kontrakt låg i sin helhet" not in page
    about = {r[0].value: r[1].value for r in load_workbook(report / "granskning.xlsx")["Om rapporten"].iter_rows()}
    assert about["Partiell granskning: AF eller kontrakt via sökning"] == "AF.pdf (2 s.)"

    assert cli.main(["run", "co-001", "t-0001", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "nor does its core, the AF and the contract" in out and "1 of them the core's" in out
    _budget(monkeypatch, full.p0 + full.p1 - 1)
    assert cli.main(["run", "co-001", "t-0001", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "its core, the AF and the contract, fits" in out and "1 priority-1 documents become search-only" in out


def test_a_lens_message_is_unchanged_when_nothing_of_priority_1_is_demoted() -> None:
    from anbud.granskning.lenses import load_lenses
    from anbud.granskning.run import LensCaps, lens_message

    lens = load_lenses()[0]
    caps = LensCaps(10, 2.0)
    assert lens_message(lens, None, caps) == lens_message(lens, None, caps, [])
    assert "search_only_priority_1" not in lens_message(lens, None, caps)
    assert "<search_only_priority_1>" in lens_message(lens, None, caps, ["aaaaaaaaaaaa"])
