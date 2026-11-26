"""The follow-up turn: one more turn in the same R2 conversation, only for marked lines the answer left unresolved."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from kalkyl_synthetic import (
    GOOD,
    cite,
    fake_sdk,
    first_block,
    good_handler,
    message,
    req,
    submit,
    text,
    write_catalog,
    write_prep,
)

from anbud import paths
from anbud.kalkyl.extract import ChunkResult, unresolved_ids
from anbud.kalkyl.lines import Chunk
from anbud.kalkyl.records import SetAside
from anbud.kalkyl.run import run_dev_tender
from anbud.kalkyl.settings import EXTRACT, load_kalkyl_config

CENTRAL = "aaaaaa-000008"  # "Ny lägenhetscentral i varje lägenhet.": a scope line
CENTRAL_RECORD = next(r for r in GOOD["aaaaaa-000001"]["records"] if r["what"] == "Lägenhetscentral")
UNCLEAR = {"block_ids": [CENTRAL], "kind": "unclear", "reason": "which central is meant"}


@pytest.fixture
def tender(data_root: Path) -> Path:
    tender = paths.dev_tender_dir("co-001", "t-0001")
    write_prep(tender / "prep")
    write_catalog(data_root)
    return tender


def _is_follow_up(params: dict[str, Any]) -> bool:
    last = params["messages"][-1]["content"]
    return isinstance(last, str) and "still unresolved" in last


def _extract_calls(sdk: Any) -> list[dict[str, Any]]:
    return [p for p in sdk.messages.sent if "tools" in p and p["tools"][0]["name"] == "submit_quantities"]


def _handler(follow_up_answer: Any):
    """The first chunk's answer leaves the central unclear; the follow-up answers with `follow_up_answer`."""

    def handler(params: dict[str, Any]) -> Any:
        if "tools" in params and params["tools"][0]["name"] == "submit_quantities":
            if first_block(params) == "aaaaaa-000001":
                if _is_follow_up(params):
                    return follow_up_answer
                records = [r for r in GOOD["aaaaaa-000001"]["records"] if r is not CENTRAL_RECORD]
                return message([submit({"records": records, "set_aside": [UNCLEAR]})])
        return good_handler(params)

    return handler


def _run(sdk: Any, run_id: str = "kalkyl-fu", **kw: Any) -> dict[str, Any]:
    return run_dev_tender("co-001", "t-0001", sdk=sdk, catalog_name="synthetic", run_id=run_id, **kw)


def test_an_unclear_line_gets_one_follow_up_that_resolves_it_and_the_first_turn_is_unchanged(tender: Path) -> None:
    sdk = fake_sdk(_handler(message([submit({"records": [CENTRAL_RECORD], "set_aside": []}, tool_id="toolu_2")])))
    summary = _run(sdk)
    assert summary["follow_ups"] == 1 and summary["follow_up_lines"] == 1 and summary["follow_up_lines_resolved"] == 1
    assert summary["lines_unresolved"] == 0
    calls = [p for p in _extract_calls(sdk) if first_block(p) == "aaaaaa-000001"]
    first, follow = calls
    assert f"1 marked line(s) are still unresolved: {CENTRAL}." in follow["messages"][-1]["content"]
    # The follow-up continues the same conversation: the first request is its unchanged prefix, so it stays cached.
    assert follow["messages"][0] == first["messages"][0] and follow["system"] == first["system"]
    assert follow["tools"] == first["tools"] and len(first["messages"]) == 1
    (accepted,) = follow["messages"][2]["content"]
    assert accepted["type"] == "tool_result" and "is_error" not in accepted
    result = json.loads((tender / "kalkyl" / "kalkyl-fu" / "result.json").read_text())
    central = next(p for p in result["posts"] if p["name"] == "Lägenhetscentral")
    assert central["quantity"] is not None  # per apartment, counted by the object model


def test_a_chunk_with_nothing_unresolved_gets_no_follow_up(tender: Path) -> None:
    sdk = fake_sdk()
    summary = _run(sdk)
    assert summary["follow_ups"] == 0 and summary["follow_up_lines"] == 0
    assert len(_extract_calls(sdk)) == summary["chunks"] and not any(_is_follow_up(p) for p in sdk.messages.sent)


def test_follow_up_items_about_other_lines_are_dropped_and_a_failed_follow_up_keeps_the_first_answer(
    tender: Path,
) -> None:
    other = req("Extra kabelstege", "stated", "m", [cite("aaaaaa-000007", "Kabelstege 45 m")], quantity=45)
    aside = {"block_ids": ["aaaaaa-000012"], "kind": "no_requirement", "reason": "a date"}
    summary = _run(fake_sdk(_handler(message([submit({"records": [other], "set_aside": [aside]})]))))
    result = json.loads((tender / "kalkyl" / "kalkyl-fu" / "result.json").read_text())
    assert "Extra kabelstege" not in {p["name"] for p in result["posts"]}
    assert summary["follow_up_lines_resolved"] == 0 and summary["lines_unresolved"] == 1  # still unclear, listed

    no_answer = message([text("Jag vet inte.")], stop_reason="end_turn")
    summary = _run(fake_sdk(_handler(no_answer)), run_id="kalkyl-fu2")
    assert summary["follow_up_errors"] == {"no_submit": 1} and summary["complete"]
    assert summary["posts"] > 0 and summary["lines_unresolved"] == 1  # the first answer stands


def test_a_rebuild_pays_only_for_the_follow_ups(tender: Path) -> None:
    config = load_kalkyl_config()
    without = replace(config, roles={**config.roles, EXTRACT: replace(config.roles[EXTRACT], follow_up=False)})
    resolve = message([submit({"records": [CENTRAL_RECORD], "set_aside": []}, tool_id="toolu_2")])
    base = _run(fake_sdk(_handler(resolve)), run_id="kalkyl-base", kalkyl=without)
    assert base["follow_ups"] == 0 and base["lines_unresolved"] == 1
    sdk = fake_sdk(_handler(resolve))
    rebuilt = _run(sdk, run_id="kalkyl-rebuild", base_run="kalkyl-base")
    assert rebuilt["follow_ups"] == 1 and rebuilt["lines_unresolved"] == 0
    assert [_is_follow_up(p) for p in _extract_calls(sdk)] == [True]  # every first turn came from the cache


def test_rows_the_object_list_counts_are_never_asked_about() -> None:
    chunk = Chunk("c001", "d", ["b1", "b2", "b3", "b4"], quantity_ids=["b1", "b2", "b3"], scope_ids=["b4"])
    result = ChunkResult("c001", "submitted", set_aside=[SetAside(block_ids=["b3"], kind="unclear", reason="?")])
    assert unresolved_ids(result, chunk) == ["b1", "b2", "b3", "b4"]
    assert unresolved_ids(result, chunk, frozenset({"b1", "b2"})) == ["b3", "b4"]
    result.set_aside.append(SetAside(block_ids=["b4"], kind="no_requirement", reason="a date"))
    assert unresolved_ids(result, chunk, frozenset({"b1", "b2"})) == ["b3"]
