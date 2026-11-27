"""The prefix budget on a generated 1000-page tender: priority 1 stays in full and under the budget, documents beyond
it become search-only in a fixed order with their outline kept, and a priority 1 that can't fit stops the run before
any model call. Synthetic documents only."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from granskning_fakes import ScriptedSDK, png_bytes, write_prep
from large_tender import DOCS, EL_LINE, TOTAL_PAGES, doc_id, write_large_prep
from test_granskning_pipeline import CLEAN, _calc, _catalog
from test_runguard import ledger_with

from anbud import cli, model_client
from anbud import config as config_module
from anbud.evals import split_heldout as sh
from anbud.granskning import provenance
from anbud.granskning.budget import PrefixBudget, apply_plan, plan_prefix, priorities
from anbud.granskning.corpus import Corpus
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


def test_a_priority_1_over_the_budget_stops_the_run_before_any_model_call(
    tender: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _budget(monkeypatch, 1000)  # less than the brief alone
    sdk = ScriptedSDK(lambda params: pytest.fail("no model call may be made"))
    monkeypatch.setattr(model_client, "keychain_key", lambda **_: "sk-test")
    monkeypatch.setattr(model_client, "make_sdk_client", lambda config, key: sdk)
    assert cli.main(["run", "co-001", "t-0001", "--run-id", "t-0001-budget"]) != 0
    err = capsys.readouterr().err
    assert "stopped: the prefix's fixed part" in err and "budget of 1000 tokens" in err
    assert sdk.sent == []  # not even a page card


def test_the_dry_run_shows_priority_1_on_its_own(
    tender: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["run", "co-001", "t-0001", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "prefix budget 450000 tokens: fixed " in out and "priority 1 fits (" in out
    _budget(monkeypatch, 1000)
    assert cli.main(["run", "co-001", "t-0001", "--dry-run"]) == 0
    assert "priority 1 does not fit: a paid run stops before any model call" in capsys.readouterr().out
