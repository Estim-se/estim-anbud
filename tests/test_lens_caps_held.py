"""The lens caps come from the tier of the stop in force, not from the measured prefix's. A run opens at the tier of the
dry run's estimate, and the map's first call can measure a prefix in a higher tier; the stop stays at the opening tier
(`Budget.set_stop` never goes above it). Caps from the higher tier let eight lenses, each within its own cap, spend the
run past that stop. Synthetic corpus, scripted fake SDK, $0. Reservations stay small (count_tokens is 1000) and the
lenses run one at a time, so what reaches the stop is settled lens spend.

Each test names the one-line mutation it catches; each was made, seen to fail the test, and reverted."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from granskning_fakes import CALC, ScriptedSDK, corpus, first_text, reply, usage
from test_granskning_run import _map

from anbud.config import load_config
from anbud.granskning.lenses import load_lenses
from anbud.granskning.run import held_tokens, lens_caps, run_granskning
from anbud.granskning.toolbox import Toolbox
from anbud.model_client import Budget, ModelClient
from anbud.store import RunStore


def _script(measured: int, turn_usd: float) -> Any:
    config = load_config()

    def answer(params: dict[str, Any]) -> Any:
        if "Map this tender" in first_text(params):
            return reply(("submit_map", _map()), use=usage(write=measured))
        # A lens that keeps searching: every turn costs `turn_usd`, well within any one lens's cap.
        out = round(turn_usd * 1_000_000 / config.price_for(params["model"]).output)
        return reply(("search_blocks", {"query": "vite"}), use=usage(input_tokens=0, output_tokens=out))

    return answer


# Opened at S ($20) and measured in M; opened at M ($30) and measured in L.
CASES = [(20.0, 160_000, 1.0), (30.0, 400_000, 1.5)]


@pytest.mark.parametrize(("opened", "measured", "turn_usd"), CASES, ids=["S-measured-M", "M-measured-L"])
def test_lenses_within_their_caps_never_reach_the_stop_the_run_is_held_at(
    data_root: Path, tmp_path: Path, opened: float, measured: int, turn_usd: float
) -> None:
    """Mutation: `lens_caps(client.config.budgets, held_tokens(...))` -> `lens_caps(client.config.budgets,
    prefix_tokens)` in `run_granskning`: the measured tier's caps; the lenses spend the run past its stop, and it halts.
    """
    store = RunStore("co-001")
    sdk = ScriptedSDK(_script(measured, turn_usd))
    client = ModelClient(load_config(), sdk, Budget(opened), run_id="r-caps", company_id="co-001", store=store)
    c = corpus(tmp_path)
    toolbox = Toolbox(c, CALC, tile_loader=store.get_blob)
    result = run_granskning(client, c, CALC, load_lenses(), toolbox=toolbox, concurrency=1)
    assert result.stop is not None and result.stop.hard_stop == opened  # held at the opening tier
    assert client.halted == 0 and client.budget.spent <= opened
    assert not [r for r in result.lenses if r.error == "BudgetExceeded"]
    assert result.prefix_tokens == measured and result.caps.max_usd * len(result.lenses) < opened


def test_the_caps_are_picked_for_the_prefix_but_never_above_the_tier_the_stop_is_held_at() -> None:
    """Mutation: `return min(prefix_tokens, int(bound))` -> `return int(bound)` in `held_tokens`: a small prefix gets
    the caps of its stop's whole tier."""
    budgets = load_config().budgets
    assert held_tokens(budgets, 160_000, 20.0) == 150_000  # measured in M, held at S
    assert held_tokens(budgets, 400_000, 30.0) == 300_000  # measured in L, held at M
    assert held_tokens(budgets, 80_000, 20.0) == 80_000  # a small prefix keeps its own, smaller caps
    assert held_tokens(budgets, 400_000, 45.0) == 400_000  # not held: the measured tier's
    assert held_tokens(budgets, 400_000, 10.0) == 150_000  # held below every tier: the lowest tier's bound
    assert lens_caps(budgets, held_tokens(budgets, 160_000, 20.0)) == lens_caps(budgets, 150_000)
