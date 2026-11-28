"""A resumed run's opening spend is what the run paid, once (audit item spend #3). Every paid call is logged as its own
`model_call` or `error` event, and those are what `RunStore.run_spend` counts. A step's aggregate event carries the
step's cost too, so a step that ended in error logs it as `step_error`, not `error`, or the resume counts it twice and
stops early. Synthetic data and fakes only, $0.

Each test names the one-line mutation it catches; each was made, seen to fail the test, and reverted."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from granskning_fakes import CALC, ScriptedSDK, corpus, first_text, good_flag, reply, usage
from test_granskning_run import _map

from anbud.config import load_config
from anbud.granskning.lenses import load_lenses
from anbud.granskning.run import GranskningResult, run_granskning
from anbud.granskning.toolbox import Toolbox
from anbud.model_client import Budget, ModelClient
from anbud.store import RunStore

RUN = "r-resume"


def _script(refuse: str) -> Callable[[dict[str, Any]], Any]:
    """The map and every lens answer, except the one whose first message contains `refuse`: it is refused after a paid
    call, so its step ends in error."""

    def script(params: dict[str, Any]) -> Any:
        text = first_text(params)
        if refuse in text:
            return reply("I can't help with that.", stop="refusal", use=usage(read=60_000))
        if "Map this tender" in text:
            return reply(("submit_map", _map()), use=usage(write=60_000))
        return reply(("submit_flags", {"flags": [good_flag()]}), use=usage(read=60_000))

    return script


def _paid_run(tmp_path: Path, refuse: str) -> tuple[RunStore, ModelClient, GranskningResult]:
    store = RunStore("co-001")
    client = ModelClient(
        load_config(), ScriptedSDK(_script(refuse)), Budget(20.0), run_id=RUN, company_id="co-001", store=store
    )
    c = corpus(tmp_path)
    result = run_granskning(client, c, CALC, load_lenses(), toolbox=Toolbox(c, CALC, tile_loader=store.get_blob))
    return store, client, result


def _resumed_spend(store: RunStore) -> float:
    return ModelClient(load_config(), None, Budget(20.0), run_id=RUN, company_id="co-001", store=store).budget.spent


def test_a_resumed_run_counts_an_errored_lens_once(data_root: Path, tmp_path: Path) -> None:
    """Mutation: `kind = "step_error" if lens_result.status == "error" else "result"` -> `kind = "error" if ...` in
    `run_granskning`: the errored lens's aggregate counts again on top of its calls."""
    store, client, result = _paid_run(tmp_path, "review lens `regulation`")
    (errored,) = [r for r in result.lenses if r.status == "error"]
    assert errored.cost_usd > 0  # the lens was refused after a paid call
    events = store.events(RUN)
    assert sum(e.cost_usd or 0 for e in events if e.kind == "model_call") == pytest.approx(client.budget.spent)
    assert _resumed_spend(store) == pytest.approx(client.budget.spent)  # once: its calls' events
    assert [e.kind for e in events if e.step == "lens:regulation"] == ["step_error"]


def test_a_resumed_run_counts_a_failed_map_once(data_root: Path, tmp_path: Path) -> None:
    """Mutation: `"result" if mapped.tender_map else "step_error"` -> `"result" if mapped.tender_map else "error"` in
    `run_granskning`: the failed map's aggregate counts again on top of its calls."""
    store, client, result = _paid_run(tmp_path, "Map this tender")
    assert result.map.tender_map is None and result.map.outcome.cost_usd > 0  # the map was refused after a paid call
    assert _resumed_spend(store) == pytest.approx(client.budget.spent)  # once: its calls' events
    assert [e.kind for e in store.events(RUN) if e.step == "map"] == ["step_error"]
