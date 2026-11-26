"""The tool loop against a scripted fake SDK: no network, no key, $0."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from granskning_fakes import CALC, ScriptedSDK, corpus, good_flag, reply, usage

from anbud.config import load_config
from anbud.granskning.loop import run_lens, with_breakpoint
from anbud.granskning.prefix import system_blocks
from anbud.granskning.toolbox import Toolbox
from anbud.model_client import Budget, ModelClient

LENS_TOOLS = frozenset({"search_blocks", "read_blocks", "outline", "calc_posts", "submit_flags"})


def _setup(tmp_path: Path, script: Any, hard_stop: float = 5.0) -> tuple[ModelClient, ScriptedSDK, Toolbox, list]:
    sdk = ScriptedSDK(script)
    client = ModelClient(load_config(), sdk, Budget(hard_stop), run_id="r-1", company_id="co-test")
    c = corpus(tmp_path)
    return client, sdk, Toolbox(c, CALC), system_blocks(c, CALC)


def _lens(client: ModelClient, toolbox: Toolbox, system: list, **kw: Any) -> Any:
    args = {"max_turns": 4, "max_usd": 1.0, "allowed": LENS_TOOLS, **kw}
    return run_lens(
        client, lens_id="lens_x", role="lens", system=system, first_message="Task.", toolbox=toolbox, **args
    )


def _results(params: dict[str, Any]) -> list[dict[str, Any]]:
    return params["messages"][-1]["content"]


def test_a_lens_searches_then_submits(tmp_path: Path) -> None:
    script = [
        reply("Looking.", ("search_blocks", {"query": "trapphus"})),
        reply(("submit_flags", {"flags": [good_flag()]})),
    ]
    client, sdk, toolbox, system = _setup(tmp_path, script)
    result = _lens(client, toolbox, system)
    assert result.status == "submitted" and result.turns == 2 and result.model == "claude-opus-5-5"
    (flag,) = result.flags
    assert flag.lens_id == "lens_x"  # the loop sets the lens id, whatever the model wrote
    search, submit = result.tool_log
    assert search.tool == "search_blocks" and search.turn == 1 and "bbbbbb-000002" in search.result_ids
    assert submit.tool == "submit_flags" and submit.error is None
    first, second = sdk.sent
    assert first["system"] == second["system"] == system and first["tools"] == second["tools"]
    # the moving breakpoint: only the newest user content carries one
    marks = [
        (i, j) for i, m in enumerate(second["messages"]) for j, b in enumerate(m["content"]) if "cache_control" in b
    ]
    assert marks == [(2, 0)] and _results(second)[0]["type"] == "tool_result"
    assert first["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_disallowed_tools_are_refused_and_cost_a_turn(tmp_path: Path) -> None:
    script = [
        reply(("submit_map", {}), ("view_tile", {"sheet": "cccccc", "tile": "p1-t1"}), ("bash", {"cmd": "ls"})),
        reply(("submit_flags", {"flags": []})),
    ]
    client, sdk, toolbox, system = _setup(tmp_path, script)
    result = _lens(client, toolbox, system)
    assert result.status == "submitted" and result.turns == 2 and result.flags == []
    refused = [c.refused for c in result.tool_log[:3]]
    assert refused == [
        "submit_map is not available to this role",
        "view_tile is not available to this lens",
        "unknown tool: bash",
    ]
    errors = _results(sdk.sent[1])
    assert [r["is_error"] for r in errors] == [True, True, True]


def test_a_failed_check_gets_one_repair_turn(tmp_path: Path) -> None:
    bad = {**good_flag(), "citations": []}
    script = [
        reply(("submit_flags", {"flags": [good_flag(), bad]})),
        reply(("submit_flags", {"flags": [good_flag("bbbbbb-000003", "regulation_ev_charging"), bad]})),
    ]
    client, sdk, toolbox, system = _setup(tmp_path, script)
    result = _lens(client, toolbox, system)
    assert result.status == "submitted" and result.turns == 2
    assert [f.claim_type for f in result.flags] == ["unpriced_requirement", "regulation_ev_charging"]
    assert [r["attempt"] for r in result.rejected] == [1, 2]  # every attempt's rejects are recorded
    assert all("citations" in r["reason"] for r in result.rejected)
    repair = _results(sdk.sent[1])[0]
    assert repair["is_error"] and "one repair" in repair["content"]
    assert [c.error for c in result.tool_log] == ["validation", None]


def test_a_lens_that_stops_gets_one_nudge(tmp_path: Path) -> None:
    client, sdk, toolbox, system = _setup(tmp_path, [reply("Done thinking."), reply(("submit_flags", {"flags": []}))])
    assert _lens(client, toolbox, system).status == "submitted"
    assert "submit_flags" in sdk.sent[1]["messages"][-1]["content"][0]["text"]

    client, sdk, toolbox, system = _setup(tmp_path / "b", [reply("No."), reply("Still no.")])
    result = _lens(client, toolbox, system)
    assert result.status == "no_submit" and result.turns == 2


def test_the_turn_cap_truncates_and_warns_first(tmp_path: Path) -> None:
    client, sdk, toolbox, system = _setup(tmp_path, lambda p: reply(("outline", {})))
    result = _lens(client, toolbox, system, max_turns=3)
    assert result.status == "truncated" and result.turns == 3 and len(result.tool_log) == 3
    notes = [b for b in _results(sdk.sent[2]) if b["type"] == "text"]
    assert notes and "last" in notes[0]["text"]


def test_the_dollar_cap_truncates(tmp_path: Path) -> None:
    expensive = usage(input_tokens=100_000, output_tokens=10_000)  # $0.60 a turn
    client, sdk, toolbox, system = _setup(tmp_path, lambda p: reply(("outline", {}), use=expensive))
    result = _lens(client, toolbox, system, max_turns=10, max_usd=1.0)
    assert result.status == "truncated" and result.turns == 2 and result.cost_usd == pytest.approx(1.2)


def test_a_model_error_ends_the_lens_as_error(tmp_path: Path) -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.RateLimitError("slow", response=httpx2.Response(429, request=request), body=None)

    def script(params: dict[str, Any]) -> Any:
        raise error

    client, sdk, toolbox, system = _setup(tmp_path, script)
    result = _lens(client, toolbox, system)
    assert result.status == "error" and result.turns == 0


def test_with_breakpoint_leaves_the_input_untouched() -> None:
    messages = [{"role": "user", "content": "hi"}]
    marked = with_breakpoint(messages)
    assert messages == [{"role": "user", "content": "hi"}]
    assert marked[0]["content"] == [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}]


def _two_claims(claim: str) -> dict:
    return {**good_flag(), "claim": claim}


def _duplicate_script() -> list:
    script = [
        reply(("submit_flags", {"flags": [_two_claims("Närvarostyrning saknas."), _two_claims("Styrning saknas.")]})),
        reply(("submit_flags", {"flags": [_two_claims("Närvarostyrning saknas."), _two_claims("Annan styrning.")]})),
    ]
    return script


def test_same_fingerprint_flags_are_combined_not_dropped(tmp_path: Path) -> None:
    client, sdk, toolbox, system = _setup(tmp_path, _duplicate_script())
    result = _lens(client, toolbox, system)
    repair = _results(sdk.sent[1])[0]
    assert repair["is_error"] and "neither is kept" in repair["content"] and "0 findings passed" in repair["content"]
    (merged,) = result.flags  # the final attempt still collides: code keeps both claims in one finding
    assert merged.claim == "Närvarostyrning saknas. / Annan styrning."
    attempts = [(r["attempt"], r["index"]) for r in result.rejected]
    assert attempts == [(1, 0), (1, 1), (2, 1)]
    assert "merged by code" in result.rejected[-1]["reason"]


def test_an_exact_repeat_is_ignored(tmp_path: Path) -> None:
    client, sdk, toolbox, system = _setup(tmp_path, [reply(("submit_flags", {"flags": [good_flag(), good_flag()]}))])
    result = _lens(client, toolbox, system)
    assert len(result.flags) == 1 and result.rejected == [] and result.turns == 1
