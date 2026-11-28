"""The leverage-weighted second reading (`kalkyl.second_reading`) on the synthetic tender, against a scripted fake SDK.
No network, no key, $0.

The fake answers a chunk's first turn from GOOD. The second identical first-turn request for the same chunk is the
second reading, and `_readings` lets a test change what either reading answers.

Each test names the one-line mutation it catches; each was made, seen to fail the test, and reverted."""

from __future__ import annotations

import copy
import json
import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import openpyxl
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
    write_catalog,
    write_prep,
)

from anbud import cli, paths, store
from anbud.evals import split_heldout as sh
from anbud.kalkyl.run import plan, run_dev_tender
from anbud.kalkyl.settings import KalkylConfig, load_kalkyl_config

Answer = dict[str, Any]
CHUNK = "aaaaaa-000001"  # the first block of the synthetic tender's chunk of requirements (c001)
LISTS = "bbbbbb-000001"  # the first block of its object list's chunk
# The leverage lines of the synthetic calc (sum 58,095 kr; 5 % is 2,905 kr):
PER_OBJECT = {"aaaaaa-000004", "aaaaaa-000008"}  # outlets (2,268 kr) and the unpriced panel, both per apartment
LARGE = {"aaaaaa-000005", "aaaaaa-000006", "aaaaaa-000007", "aaaaaa-000010"}  # 5 % or more; the last is the option


@pytest.fixture
def tender(data_root: Path) -> Path:
    tender = paths.dev_tender_dir("co-001", "t-0001")
    write_prep(tender / "prep")
    write_catalog(data_root)
    return tender


def _on() -> KalkylConfig:
    return replace(load_kalkyl_config(), second_reading=True)


def _first_turn(params: dict[str, Any]) -> bool:
    tools = params.get("tools") or [{}]
    return tools[0].get("name") == "submit_quantities" and len(params["messages"]) == 1


def _readings(
    first: Callable[[Answer], Answer] = lambda a: a, second: Callable[[Answer], Answer] = lambda a: a
) -> tuple[Callable[[dict[str, Any]], Any], Counter[str]]:
    """A handler whose first and second first-turn answers for the requirements chunk are changed by `first` and
    `second`, and the count of first-turn requests per chunk."""
    seen: Counter[str] = Counter()
    lock = threading.Lock()

    def handler(params: dict[str, Any]) -> Any:
        if not _first_turn(params):
            return good_handler(params)
        block = first_block(params) or ""
        with lock:
            seen[block] += 1
            n = seen[block]
        if block != CHUNK:
            return good_handler(params)
        change = first if n == 1 else second
        return message([submit(change(copy.deepcopy(GOOD[CHUNK])))])

    return handler, seen


def _record(what: str, **changes: Any) -> Callable[[Answer], Answer]:
    def change(answer: Answer) -> Answer:
        for record in answer["records"]:
            if record["what"] == what:
                record.update({k: v for k, v in changes.items() if v is not None})
                for k in (k for k, v in changes.items() if v is None):
                    record.pop(k, None)
        return answer

    return change


def _run(handler: Callable[[dict[str, Any]], Any] | None, kalkyl: KalkylConfig | None = None, **kw: Any) -> Answer:
    sdk = fake_sdk(handler) if handler is not None else None
    return run_dev_tender(
        "co-001", "t-0001", sdk=sdk, catalog_name="synthetic", run_id="kalkyl-test", kalkyl=kalkyl or _on(), **kw
    )


def _result(tender: Path, run_id: str = "kalkyl-test") -> Answer:
    return json.loads((tender / "kalkyl" / run_id / "result.json").read_text())


def _sheet(tender: Path, name: str, run_id: str = "kalkyl-test") -> list[tuple[Any, ...]]:
    wb = openpyxl.load_workbook(tender / "kalkyl" / run_id / "kalkyl.xlsx")
    return [row for row in wb[name].iter_rows(min_row=5, values_only=True) if any(row)]


def _post(result: Answer, name: str) -> Answer:
    return next(p for p in result["posts"] if p["name"] == name)


def test_a_line_read_differently_in_kind_becomes_a_question_and_the_calc_keeps_the_first_reading(
    tender: Path,
) -> None:
    """Mutation: `first[block_id].kinds != second[block_id].kinds` -> `first[block_id].kinds == second[block_id].kinds`
    in `compare`: the five agreed lines become questions and the one the readings differ on doesn't."""
    handler, _ = _readings(second=_record("Dubbla vägguttag i sovrum", quantity_source="stated", per_object=None))
    summary = _run(handler)
    assert summary["second_reading"]["questions"] == 1 and summary["second_reading"]["agreed"] == 5
    result = _result(tender)
    outlets = _post(result, "Dubbla vägguttag i sovrum")
    assert Decimal(outlets["quantity"]) == 6 and Decimal(outlets["total"]) == 2268  # the first reading: 2 x 3
    assert Decimal(result["totals"]["total"]) == Decimal("58095.00")  # the same sum as one reading
    assert any(n.startswith("Fråga till kalkylatorn: två läsningar av aaaaaa-000004") for n in outlets["notes"])
    (row,) = _sheet(tender, "Frågor")
    assert row[0] == "aaaaaa-000004" and row[1] == outlets["id"]
    assert row[4].startswith("per objekt (lägenhet), 2 st") and row[6].startswith("angiven mängd, 2 st")
    assert row[5] == row[7] == "2 st dubbla vägguttag"  # both readings' quotes
    assert result["second_reading"]["questions"] == [
        {
            "block_id": "aaaaaa-000004",
            "posts": [outlets["id"]],
            "first": ["per_object:apartment:included"],
            "second": ["stated:included"],
        }
    ]
    overview = {r[0]: r[1] for r in _sheet(tender, "Översikt")}
    assert overview["Andra läsning av rader med hävstång (se Frågor)"] == "6 rader i 1 avsnitt: 5 överens, 1 frågor"


def test_a_record_the_second_reading_sets_aside_is_a_question(tender: Path) -> None:
    """Mutation: `item.kind != "unclear"` -> `item.kind == "unclear"` in `decisions`: set-asides go unseen, and the
    line counts as unanswered instead of a question."""

    def other_trade(answer: Answer) -> Answer:
        answer["records"] = [r for r in answer["records"] if r["what"] != "Lägenhetscentral"]
        answer["set_aside"] = [{"block_ids": ["aaaaaa-000008"], "kind": "other_trade", "reason": "levereras av annan"}]
        return answer

    handler, _ = _readings(second=other_trade)
    summary = _run(handler)
    assert summary["second_reading"]["questions"] == 1 and summary["second_reading"]["unanswered"] == 0
    (q,) = _result(tender)["second_reading"]["questions"]
    assert q["block_id"] == "aaaaaa-000008" and q["second"] == ["set_aside"]
    (row,) = _sheet(tender, "Frågor")
    assert row[6] == "bortvald: Annan entreprenör eller beställaren (levereras av annan)"


def test_a_set_aside_the_second_reading_multiplies_is_asked_too(tender: Path) -> None:
    """Mutation: `asked = [b for b in lines if chunk_of.get(b) in done] + multiplied_set_asides(first, second)` ->
    `asked = [b for b in lines if chunk_of.get(b) in done]`: a line the first reading set aside and the second
    multiplies per stairwell is never compared."""
    scope = "aaaaaa-000002"  # "... ett flerbostadshus med tre trapphus."

    def set_aside(answer: Answer) -> Answer:
        answer["set_aside"] = [{"block_ids": [scope], "kind": "no_requirement", "reason": "beskrivning av huset"}]
        return answer

    def per_stairwell(answer: Answer) -> Answer:
        answer["records"].append(
            req(
                "Belysning per trapphus",
                "per_object",
                "st",
                [cite(scope, "tre trapphus")],
                quantity=1,
                per_object="stairwell",
            )
        )
        return answer

    handler, _ = _readings(first=set_aside, second=per_stairwell)
    summary = _run(handler)
    assert summary["second_reading"]["lines"] == 7 and summary["second_reading"]["questions"] == 1
    (q,) = _result(tender)["second_reading"]["questions"]
    assert q == {"block_id": scope, "posts": [], "first": ["set_aside"], "second": ["per_object:stairwell:included"]}
    (row,) = _sheet(tender, "Frågor")
    assert row[1] is None and row[6].startswith("per objekt (trapphus)")  # no post: the first reading has none


def test_quantities_alone_never_make_a_question(tender: Path) -> None:
    """Mutation: `record.scope])` -> `record.scope, str(record.quantity)])` in `_kind`: two readings that agree on
    what kind of line it is but count it differently become questions.

    The second reading leaves out the panel's count per apartment ("i varje lägenhet"), as the brief allows, so code
    applies its assumed count: the same kind of line with another quantity. (A quantity its quote doesn't write fails
    the checks, so it never reaches the comparison.)"""
    handler, _ = _readings(second=_record("Lägenhetscentral", quantity=None))
    summary = _run(handler)
    assert summary["second_reading"] | {"cost_usd": 0} == {
        "lines": 6,
        "chunks": 1,
        "calls": 1,
        "cost_usd": 0,
        "agreed": 6,
        "questions": 0,
        "unanswered": 0,
        "errors": 0,
        "halted": None,
    }
    assert _sheet(tender, "Frågor") == []


def test_only_the_chunks_of_leverage_lines_are_read_again(tender: Path) -> None:
    """Mutation: `multiplies = any(ln.per for ln in post.lines)` -> `multiplies = False` in `leverage`: the outlets
    (below 5 % of the sum) and the unpriced panel, both multiplied per apartment, are no longer read again."""
    handler, seen = _readings()
    _run(handler)
    second = _result(tender)["second_reading"]
    assert set(second["lines"]) == PER_OBJECT | LARGE
    assert second["chunks"] == ["c001"]
    assert seen == {CHUNK: 2, LISTS: 1}  # the object list's chunk holds no leverage line: read once


def test_the_second_reading_is_a_fresh_sample_and_a_rebuild_answers_it_at_no_cost(tender: Path) -> None:
    """Mutation: `SAMPLE = 1` -> `SAMPLE = 0`: the second reading is the first turn's own cache entry, so it always
    agrees and never reaches the model."""
    handler, seen = _readings(second=_record("Dubbla vägguttag i sovrum", quantity_source="stated", per_object=None))
    paid = _run(handler)
    assert seen[CHUNK] == 2 and paid["second_reading"]["questions"] == 1
    base = tender / "kalkyl" / "kalkyl-test"
    for run_id, kalkyl in (("kalkyl-rebuild", _on()), ("kalkyl-rebuild-one", load_kalkyl_config())):
        summary = run_dev_tender(
            "co-001",
            "t-0001",
            sdk=None,
            catalog_name="synthetic",
            run_id=run_id,
            base_run="kalkyl-test",
            kalkyl=kalkyl,
        )
        assert summary["complete"] and summary["cost_usd"] == 0 and summary["replay_misses"] == 0
    rebuilt, first = _result(tender, "kalkyl-rebuild")["second_reading"], _result(tender)["second_reading"]
    assert rebuilt["summary"]["cost_usd"] == 0 and rebuilt["summary"]["calls"] == 1  # answered from the cache
    assert {**rebuilt, "summary": None} == {**first, "summary": None}
    assert {**rebuilt["summary"], "cost_usd": 0} == {**first["summary"], "cost_usd": 0}
    assert (tender / "kalkyl" / "kalkyl-rebuild" / "posts.json").read_bytes() == (base / "posts.json").read_bytes()
    assert "second_reading" not in _result(tender, "kalkyl-rebuild-one")  # without it, a rebuild never asks


def test_a_run_without_it_makes_no_second_call_and_writes_no_fragor_sheet(tender: Path) -> None:
    """Mutation: `if kalkyl.second_reading and not halted:` -> `if not halted:` in `run_kalkyl`: every run reads its
    leverage lines twice and pays for it."""
    assert load_kalkyl_config().second_reading is False  # shipped off
    handler, seen = _readings()
    summary = _run(handler, kalkyl=load_kalkyl_config())
    assert seen == {CHUNK: 1, LISTS: 1}
    assert "second_reading" not in summary and "second_reading" not in _result(tender)
    wb = openpyxl.load_workbook(tender / "kalkyl" / "kalkyl-test" / "kalkyl.xlsx")
    assert "Frågor" not in wb.sheetnames


def test_a_stop_in_the_second_reading_marks_the_run_incomplete_and_resuming_finishes_it(tender: Path) -> None:
    """Mutation: `return read and (self.second is None or self.second.halted is None)` -> `return read` in
    `KalkylResult.complete`: a run stopped before its second reading counts as finished and is frozen half read."""
    handler, seen = _readings()

    def stop_after_r3(params: dict[str, Any]) -> Any:
        if params["tools"][0]["name"] == "submit_matches":  # R3: the last call before the second reading
            store.request_stop("co-001", "kalkyl-test")
        return handler(params)

    stopped = _run(stop_after_r3)
    assert not stopped["complete"] and not stopped["frozen"]
    assert stopped["second_reading"] | {"cost_usd": 0} == {
        "lines": 0,
        "chunks": 1,
        "calls": 0,
        "cost_usd": 0,
        "agreed": 0,
        "questions": 0,
        "unanswered": 0,
        "errors": 1,
        "halted": "stopped",
    }
    overview = {r[0]: r[1] for r in _sheet(tender, "Översikt")}
    assert overview["Andra läsning av rader med hävstång (se Frågor)"].endswith(
        "(1 avsnitt inte lästa igen: körningen stoppades)"
    )
    store.request_stop("co-001", "kalkyl-test", clear=True)
    resumed = _run(handler)  # the same run id: the first reading from the cache, the second read now
    assert resumed["complete"] and resumed["second_reading"]["agreed"] == 6 and seen[CHUNK] == 2


def test_the_dry_run_counts_the_second_reading_in_the_high_estimate(tender: Path, capsys) -> None:
    """Mutation: `+ extra.get("second_reading_cost_usd_est_high", 0)` -> `+ 0` in `plan`: the run would pick its
    hard stop's tier from an estimate that leaves out the second reading."""
    off = plan("co-001", "t-0001", catalog_name="synthetic", kalkyl=load_kalkyl_config())
    on = plan("co-001", "t-0001", catalog_name="synthetic", kalkyl=_on())
    assert "second_reading_calls_max" not in off and on["second_reading_calls_max"] == on["chunks"] == 2
    assert on["second_reading_cost_usd_est_high"] > 0
    assert on["cost_usd_est_high"] == pytest.approx(
        off["cost_usd_est_high"] + on["second_reading_cost_usd_est_high"], abs=0.011
    )
    sh.append_events(paths.ledger_path("co-001"), [sh._assign("t-0001", sh.DEV, "test", ())])
    assert cli.main(["kalkyl", "co-001", "t-0001", "--catalog", "synthetic", "--dry-run", "--second-reading"]) == 0
    assert "\nsecond_reading_calls_max: 2\n" in capsys.readouterr().out
