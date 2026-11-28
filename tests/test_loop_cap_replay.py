"""A tool loop's dollar cap counts each call at what it cost when it was made, a cache hit included. A cache hit costs
the run $0, so a loop that summed what the run spent decided its "last turn" note and its truncation differently on a
replay (or a resume) than in the paid run: a different request, a step-cache miss, outputs that differ. The budget
still charges a cache hit $0. Synthetic corpus, scripted fake SDK, $0.

Each test names the one-line mutation it catches; each was made, seen to fail the test, and reverted."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from granskning_fakes import CALC, ScriptedSDK, corpus, reply, usage

from anbud.config import load_config
from anbud.granskning.loop import run_lens
from anbud.granskning.prefix import system_blocks
from anbud.granskning.toolbox import Toolbox
from anbud.model_client import Budget, ModelClient
from anbud.store import RunStore

TOOLS = frozenset({"search_blocks", "read_blocks", "outline", "calc_posts", "submit_flags"})
COMPANY = "co-001"
LAST = "Your next turn is your last."
EXPENSIVE = usage(input_tokens=100_000, output_tokens=10_000)  # $0.60 a turn: turn 2 gets the last-turn note


def _lens(client: ModelClient, tmp_path: Path) -> Any:
    c = corpus(tmp_path)
    return run_lens(
        client,
        lens_id="lens_x",
        role="lens",
        system=system_blocks(c, CALC),
        first_message="Task.",
        toolbox=Toolbox(c, CALC),
        max_turns=10,
        max_usd=1.0,
        allowed=TOOLS,
    )


def _client(store: RunStore, sdk: Any, run_id: str, salt: str | None = None) -> ModelClient:
    return ModelClient(load_config(), sdk, Budget(50.0), run_id=run_id, company_id=COMPANY, store=store, salt=salt)


def _paid_then_replay(tmp_path: Path, script: Any) -> tuple[Any, Any, ModelClient]:
    store = RunStore(COMPANY, root=tmp_path / "data")
    first = _lens(_client(store, ScriptedSDK(script), "r-paid"), tmp_path / "c")
    # `anbud run --replay --base-run r-paid --run-id r-replay`: no SDK, the base run's salt, the same store
    replay = _client(store, None, "r-replay", salt=store.stored_salt("r-paid"))
    again = _lens(replay, tmp_path / "c")
    return first, again, replay


def _notes(params: dict[str, Any]) -> int:
    return sum(LAST in str(m["content"]) for m in params["messages"])


def test_a_lens_warned_by_its_dollar_cap_replays_at_zero(tmp_path: Path) -> None:
    """Mutation: `capped += result.loop_cost_usd` -> `capped += result.cost_usd` in `run_tool_loop`: on the replay the
    cached turn counts $0, the note is missing from turn 2's request, and the step cache misses."""
    first, again, replay = _paid_then_replay(tmp_path, lambda p: reply(("outline", {}), use=EXPENSIVE))
    assert first.status == "truncated" and first.turns == 2 and first.cost_usd > 1.0
    assert replay.replay_misses == 0, f"replay missed: status={again.status} turns={again.turns} error={again.error}"
    assert (again.status, again.turns, len(again.tool_log)) == (first.status, first.turns, len(first.tool_log))
    assert again.cost_usd == 0.0 and replay.budget.spent == 0.0  # the replay itself still costs nothing


def test_a_lens_truncated_by_its_dollar_cap_without_a_note_replays_at_zero(tmp_path: Path) -> None:
    """Turn 1 is cheap (no note); turn 2 alone crosses the cap. Mutation: `return self.cost_usd if self.paid_usd is
    None else self.paid_usd` -> `return self.cost_usd` in `CallResult.loop_cost_usd`: the replay asks for a third turn
    the paid run never made, and misses."""
    cheap = usage(input_tokens=1_000, output_tokens=100)
    expensive = usage(input_tokens=200_000, output_tokens=20_000)
    turns = iter([cheap, expensive])
    first, again, replay = _paid_then_replay(tmp_path, lambda p: reply(("outline", {}), use=next(turns)))
    assert first.status == "truncated" and first.turns == 2 and first.cost_usd > 1.0
    assert replay.replay_misses == 0, f"replay missed: status={again.status} turns={again.turns} error={again.error}"
    assert (again.status, again.turns) == (first.status, first.turns)


class _Killed(BaseException):
    """The process dies mid-run (a kill, Ctrl-C): the paid run stops after its first turn. A BaseException, as a kill
    or an interrupt is: an Exception raised inside the SDK's call is a failed call, tried again (`stream`)."""


class _DiesAt(ScriptedSDK):
    def __init__(self, at: int) -> None:
        super().__init__(lambda p: reply(("outline", {}), use=EXPENSIVE))
        self.at = at

    def stream(self, **params: Any) -> Any:
        if len(self.sent) + 1 == self.at:
            raise _Killed
        return super().stream(**params)


def test_a_resumed_lens_counts_its_cached_turns_against_its_cap(tmp_path: Path) -> None:
    """A paid run dies after turn 1; resumed with the same run id, turn 1 comes from the step cache and turn 2 is sent
    as the uninterrupted run sent it, note and all. Mutation: `paid = float(record.get("cost_usd") or 0.0)` ->
    `paid = 0.0` in `_cached`: the resumed loop counts turn 1 at $0, sends turn 2 without the note, and pays for turns
    the uninterrupted run never made."""
    whole = ScriptedSDK(lambda p: reply(("outline", {}), use=EXPENSIVE))
    uninterrupted = _lens(_client(RunStore(COMPANY, root=tmp_path / "a"), whole, "r-1"), tmp_path / "c")

    store = RunStore(COMPANY, root=tmp_path / "b")
    with pytest.raises(_Killed):
        _lens(_client(store, _DiesAt(2), "r-1"), tmp_path / "c")
    again = ScriptedSDK(lambda p: reply(("outline", {}), use=EXPENSIVE))
    resumed = _lens(_client(store, again, "r-1"), tmp_path / "c")  # paid, same run: the salt is read back
    assert (resumed.status, resumed.turns) == (uninterrupted.status, uninterrupted.turns) == ("truncated", 2)
    assert len(again.sent) == 1 and _notes(again.sent[0]) == _notes(whole.sent[1]) == 1
