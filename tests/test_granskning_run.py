"""Map, lens files and orchestration against a scripted fake SDK, with a real run store in a temp dir."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from granskning_fakes import (
    CALC,
    DOC_AF,
    DOC_EL,
    ScriptedSDK,
    corpus,
    first_text,
    good_flag,
    reply,
    usage,
    write_prep,
)
from test_runguard import ledger_with

from anbud import cli, model_client
from anbud.config import load_config
from anbud.evals import split_heldout as sh
from anbud.granskning import command
from anbud.granskning.contracts import LensResult
from anbud.granskning.lenses import LensFileError, load_lenses, parse_lens
from anbud.granskning.mapping import check_map, run_map
from anbud.granskning.prefix import system_blocks
from anbud.granskning.run import lens_caps, run_granskning
from anbud.granskning.toolbox import Toolbox
from anbud.model_client import Budget, ModelClient
from anbud.models import TenderMap
from anbud.store import RunStore
from anbud.tools import ROLES

FOCUS = ["Hur stort är etappvitet?", "Finns tak för vitet?", "Hur många etapper?"]


def _map(block_id: str = "aaaaaa-000002") -> dict[str, Any]:
    return {
        "documents": [{"doc_id": DOC_AF, "role": "AF"}, {"doc_id": DOC_EL, "role": "teknisk beskrivning el"}],
        "stated_scope": "Elarbeten i ett påhittat flerbostadshus.",
        "lenses": [{"lens_id": "contract_terms", "focus_questions": FOCUS}],
        "citations": [{"block_id": block_id, "quote": "Etappvite"}],
    }


def test_the_lens_files_parse_and_stay_inside_their_roles() -> None:
    lenses = load_lenses()
    ids = {lens.id for lens in lenses}
    assert ids == {
        "contract_terms",
        "trade_boundaries",
        "document_conflicts",
        "regulation",
        "occupied_building",
        "missing_documents",
        "quantities_hours",
        "generic_scope",
    }
    for lens in lenses:
        assert lens.tools <= ROLES[lens.role].tools and "submit_flags" in lens.tools
        assert "## Checklist" in lens.body and "## What counts as material" in lens.body and "## Tools" in lens.body
    generic = [lens for lens in lenses if lens.generic]
    assert [g.id for g in generic] == ["generic_scope"] and generic[0].role == "lens_generic_scope"


def test_a_bad_lens_file_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "x"
    folder.mkdir()
    path = folder / "SKILL.md"
    path.write_text("---\nname: x\ndescription: d\nrole: lens\ntools: [bash, submit_flags]\n---\nBody\n")
    with pytest.raises(LensFileError, match="may not call"):
        parse_lens(path)
    path.write_text("---\nname: y\ndescription: d\nrole: lens\ntools: [submit_flags]\n---\nBody\n")
    with pytest.raises(LensFileError, match="folder"):
        parse_lens(path)


def test_the_map_must_cite_real_blocks_and_known_lenses(tmp_path: Path) -> None:
    c = corpus(tmp_path)
    good = TenderMap.model_validate(_map())
    assert check_map(good, c, {"contract_terms"}) == []
    bad = TenderMap.model_validate(
        {**_map("nope"), "documents": [{"doc_id": "zzz", "role": "AF"}], "lenses": [{**_map()["lenses"][0]}] * 2}
    )
    problems = check_map(bad, c, {"other"})
    assert any("unknown block id nope" in p for p in problems)
    assert any("unknown document id zzz" in p for p in problems)
    assert any("unknown lens id contract_terms" in p for p in problems)
    assert any("planned twice" in p for p in problems)


def test_the_map_gets_one_repair(tmp_path: Path) -> None:
    sdk = ScriptedSDK([reply(("submit_map", _map("nope"))), reply(("submit_map", _map()))])
    client = ModelClient(load_config(), sdk, Budget(5.0), run_id="r-1", company_id="co-test")
    c = corpus(tmp_path)
    result = run_map(client, system=system_blocks(c, CALC), corpus=c, toolbox=Toolbox(c, CALC), lenses=load_lenses())
    assert result.tender_map is not None and result.outcome.turns == 2 and result.problems == []
    assert "unknown block id nope" in sdk.sent[1]["messages"][-1]["content"][0]["content"]


def _script(params: dict[str, Any]) -> Any:
    text = first_text(params)
    if "Map this tender" in text:
        return reply(("submit_map", _map()), use=usage(write=60_000))
    return reply(("submit_flags", {"flags": [good_flag()]}), use=usage(read=60_000))


def _run(data_root: Path, tmp_path: Path, sdk: Any, run_id: str = "t-0001-a") -> tuple[Any, RunStore, ModelClient]:
    store = RunStore("co-001")
    client = ModelClient(load_config(), sdk, Budget(20.0), run_id=run_id, company_id="co-001", store=store)
    c = corpus(tmp_path)
    toolbox = Toolbox(c, CALC, tile_loader=store.get_blob)
    return run_granskning(client, c, CALC, load_lenses(), toolbox=toolbox, concurrency=3), store, client


def test_a_run_maps_then_runs_every_lens_and_records_it(data_root: Path, tmp_path: Path) -> None:
    sdk = ScriptedSDK(_script)
    result, store, client = _run(data_root, tmp_path, sdk)
    assert result.map.tender_map is not None and result.prefix_tokens == 60_000
    assert result.caps is not None and (result.caps.max_turns, result.caps.max_usd) == (8, 1.5)
    assert len(result.lenses) == 8 and {r.status for r in result.lenses} == {"submitted"}
    assert all(len(r.flags) == 1 for r in result.lenses)
    assert next(r for r in result.lenses if r.lens_id == "generic_scope").generic

    opus = [p for p in sdk.sent if p["model"] == "claude-opus-5-5"]
    sonnet = [p for p in sdk.sent if p["model"] == "claude-sonnet-5-5"]
    assert len(opus) == 8 and len(sonnet) == 1
    assert all(p["system"] == opus[0]["system"] and p["tools"] == opus[0]["tools"] for p in sdk.sent)
    contract = next(p for p in sdk.sent if "review lens `contract_terms`" in first_text(p))
    assert all(q in first_text(contract) for q in FOCUS) and '"stated_scope"' in first_text(contract)

    summary = json.loads((store.run_dir("t-0001-a") / "granskning.json").read_text())
    assert summary["map"]["valid"] and len(summary["lenses"]) == 8
    assert summary["map"]["error"] is None and summary["lenses"][0]["error"] is None
    blob = summary["lenses"][0]["blob"]
    assert LensResult.model_validate_json(store.get_blob(blob)).status == "submitted"
    kinds = [(e.step, e.kind) for e in store.events("t-0001-a") if e.kind != "model_call"]
    assert kinds[0] == ("granskning", "start") and kinds[-1] == ("granskning", "end")
    assert ("map", "result") in kinds and sum(1 for s, k in kinds if s.startswith("lens:")) == 8
    assert result.cost_usd == pytest.approx(client.budget.spent)


def test_a_finished_run_replays_from_the_step_cache_at_zero_cost(data_root: Path, tmp_path: Path) -> None:
    first, _, _ = _run(data_root, tmp_path, ScriptedSDK(_script))
    again, _, client = _run(data_root, tmp_path, None)  # no SDK: any call not in the cache is refused
    assert again.cost_usd == 0 and client.budget.spent == pytest.approx(first.cost_usd)
    assert [r.flags for r in again.lenses] == [r.flags for r in first.lenses]
    assert again.map.tender_map == first.map.tender_map


def test_lens_caps_follow_the_prefix_tiers() -> None:
    budgets = load_config().budgets
    assert lens_caps(budgets, 85_000).max_turns == 8
    assert lens_caps(budgets, 120_000).max_usd == 2.0
    assert lens_caps(budgets, 5_000_000).max_turns == 12


def test_cli_run_needs_a_prepared_tender_and_never_reads_the_key_for_replay(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_key(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("the key must not be read")

    monkeypatch.setattr(model_client, "keychain_key", no_key)
    ledger_with(data_root, t_0001=sh.DEV)
    assert cli.main(["run", "co-001", "t-0001"]) == cli.REFUSED  # nothing prepared yet
    write_prep(data_root / "co-001" / "dev" / "t-0001" / "prep")
    assert cli.main(["run", "co-001", "t-0001", "--replay"]) == cli.REFUSED  # replay needs a run id
    replay = ["run", "co-001", "t-0001", "--replay", "--run-id", "t-0001-x", "--lens", "regulation"]
    assert cli.main(replay) == command.REPLAY_INCOMPLETE  # nothing is cached for this run
