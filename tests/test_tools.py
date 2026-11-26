"""The shared tool list, its snapshot, and the role table."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from anbud import schema_check, tools
from anbud.schema import complexity

ROOT = Path(__file__).resolve().parents[1]


def test_the_list_has_six_strict_read_tools_and_three_submit_tools() -> None:
    shared = tools.shared_tools()
    assert [t["name"] for t in shared] == [
        "search_blocks", "read_blocks", "outline", "calc_posts", "law_lookup", "view_tile",
        "submit_map", "submit_flags", "submit_verdict",
    ]  # fmt: skip
    assert [t.get("strict", False) for t in shared] == [True] * 6 + [False] * 3


def test_the_list_stays_inside_strict_mode_limits() -> None:
    report = complexity(tools.shared_tools())
    assert report.ok, (report.summary(), report.problems)


def test_the_committed_snapshot_matches_the_code() -> None:
    snapshot = (ROOT / "schemas" / "tools.json").read_text()
    assert snapshot == tools.snapshot_text(), "run `uv run anbud schema-check --write` and commit schemas/tools.json"


def test_the_list_is_the_same_on_every_call_and_callers_get_a_copy() -> None:
    first = tools.shared_tools()
    first[0]["description"] = "changed"
    assert tools.shared_tools()[0]["description"] != "changed"
    assert tools.shared_tools() == tools.shared_tools()


def test_strict_tools_require_every_field_without_a_default() -> None:
    for tool in tools.shared_tools():
        if not tool.get("strict"):
            continue
        model = tools.INPUT_MODELS[tool["name"]]
        needed = {name for name, f in model.model_fields.items() if f.is_required()}
        assert set(tool["input_schema"].get("required", [])) == needed, tool["name"]


def test_constraints_dropped_from_strict_schemas_are_still_enforced_in_code() -> None:
    with pytest.raises(ValidationError):
        tools.ReadBlocksInput(ids=[f"b{i}" for i in range(21)])
    with pytest.raises(ValidationError):
        tools.SearchBlocksInput(query="")
    with pytest.raises(ValidationError):
        tools.SearchBlocksInput.model_validate({"query": "el", "extra": 1})
    assert tools.OutlineInput.model_validate({}).doc_id is None


def test_the_role_table_matches_the_config_and_the_list() -> None:
    providers = tomllib.loads((ROOT / "config" / "providers.toml").read_text())
    assert set(tools.ROLES) == set(providers["roles"])
    used = set().union(*(role.tools for role in tools.ROLES.values()))
    assert used == set(tools.TOOL_NAMES)  # every tool has a role, and no role names a tool that doesn't exist
    assert tools.ROLES["verifier"].tools == {"read_blocks", "search_blocks", "calc_posts", "submit_verdict"}


def test_a_role_is_limited_in_the_loop() -> None:
    assert tools.refusal("verifier", "read_blocks") is None
    assert tools.refusal("verifier", "law_lookup") == "law_lookup is not available to this role"
    assert tools.refusal("map", "submit_flags") is not None
    assert tools.refusal("lens", "run_shell") == "unknown tool: run_shell"


def test_schema_check_passes_offline(capsys: pytest.CaptureFixture[str]) -> None:
    assert schema_check.main([]) == 0
    out = capsys.readouterr().out
    assert "9 tools" in out and "strict tools 6/20" in out
