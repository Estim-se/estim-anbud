"""The code-group pass: each code group of the technical description that nothing covers after R2 is read on its own,
twice, and only what both readings agree on reaches the calc. The AF is never read. Every heading here is invented
("Påhittad rubrik"); no AMA or AF title is used."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

import openpyxl
from kalkyl_synthetic import cite, fake_sdk, first_block, req, submit, write_catalog
from kalkyl_synthetic import message as kalkyl_message
from test_code_groups import _handler as coded_handler
from test_code_groups import write_coded_prep

from anbud import paths
from anbud.kalkyl.extract import ChunkResult
from anbud.kalkyl.group_pass import (
    ASK,
    NO_WORK,
    OUTCOME_LABELS,
    QUESTION,
    READ_IN,
    RECORDS,
    GroupResult,
    agree,
    classify_reading,
)
from anbud.kalkyl.lines import Chunk
from anbud.kalkyl.records import QuantityRecord
from anbud.kalkyl.settings import load_kalkyl_config

A = "aaaaaa-{:06d}".format
PER_ROOM = req("kanalisation", "per_object", "st", [cite(A(14), "förläggs dolt i alla bostadsrum")], per_object="room")
STATED = req("rör", "stated", "m", [cite(A(16), "Rören utförs enligt ritningen")], quantity=1)
HEADING_RECORD = req("arbete", "missing", "st", [cite(A(1), "63.B Påhittad rubrik")])


def _aside(ids: list[str], kind: str, reason: str = "allmänt krav") -> dict[str, Any]:
    return {"block_ids": ids, "kind": kind, "reason": reason}


# Each group's two readings, keyed by the group chunk's first block.
SUB_CODES = _aside([A(15), A(16), A(17), A(18)], "no_requirement")  # 63.EB and 63.EC, set aside in both
READINGS = {
    A(13): [{"records": [PER_ROOM], "set_aside": [SUB_CODES]}] * 2,  # agree: records, and the rest set aside
    A(19): [{"records": [], "set_aside": [_aside([A(19), A(20)], "no_requirement")]}] * 2,  # agree: no priced work
    A(1): [  # records against a set-aside: a question
        {"records": [HEADING_RECORD], "set_aside": []},
        {"records": [], "set_aside": [_aside([A(1)], "heading_or_contents")]},
    ],
}


class Handler:
    """R2 as in test_code_groups; a group turn answers its readings in order. Records every group turn."""

    def __init__(self, readings: dict[str, list[dict[str, Any]]] = READINGS) -> None:
        self.readings = readings
        self.seen: Counter[str] = Counter()

    def __call__(self, params: dict[str, Any]) -> Any:
        content = params["messages"][0]["content"] if "tools" in params else ""
        if isinstance(content, str) and ASK in content:
            first = first_block(params) or ""
            n = self.seen[first]
            self.seen[first] += 1
            return kalkyl_message([submit(self.readings[first][n])])
        return coded_handler(params)


def _run(data_root: Path, handler: Handler, run_id: str, *, on: bool = True) -> tuple[dict[str, Any], Path]:
    from anbud.kalkyl.run import run_dev_tender

    tender = paths.dev_tender_dir("co-001", "t-0001")
    write_coded_prep(tender / "prep")
    write_catalog(data_root)
    kalkyl = replace(load_kalkyl_config(), group_pass=on)
    sdk = fake_sdk(handler)
    summary = run_dev_tender("co-001", "t-0001", sdk=sdk, catalog_name="synthetic", run_id=run_id, kalkyl=kalkyl)
    return summary, tender / "kalkyl" / run_id


def test_each_uncovered_technical_group_is_read_twice_and_the_af_never(data_root: Path) -> None:
    handler = Handler()
    summary, out = _run(data_root, handler, "k-pass")
    assert handler.seen == {A(1): 2, A(13): 2, A(19): 2}  # 63.B, 63.E, YGB.1; not AFB
    assert summary["group_pass"] | {} == {
        **summary["group_pass"],
        "groups": 3,
        "to_records": 1,
        "no_priced_work": 1,
        "questions": 1,
        "not_read": 0,
        "records_added": 1,
        "calls": 6,
        "posts_added": 1,
    }
    # after the pass, only the question and the AF group are still without a post or a reason
    assert summary["code_groups"]["teknisk"]["rows"] == 1 and summary["code_groups"]["af"]["rows"] == 1
    body = json.loads((out / "result.json").read_text())
    record = next(r for r in body["records"] if r["chunk_id"].startswith("g-"))
    assert record["id"] == "g-aaaaaa-000013-r001" and record["record"]["per_object"] == "room"
    assert any(record["id"] in p["records"] for p in body["posts"])  # the normal record path: counted and posted
    assert [g["outcome"] for g in body["group_pass"]["groups"]] == [QUESTION, RECORDS, NO_WORK]


def test_the_kodgrupper_sheet_shows_what_the_pass_decided_and_both_readings(data_root: Path) -> None:
    _, out = _run(data_root, Handler(), "k-sheet")
    wb = openpyxl.load_workbook(out / "kalkyl.xlsx")
    rows = {r[0]: r for r in ([c.value for c in row] for row in wb["Kodgrupper"].iter_rows(min_row=5))}
    assert list(rows) == ["63.B", "63.E", "YGB.1", "AFB"]  # the pass's groups in reading order, then the AF
    assert rows["63.E"][9:11] == ["Post från kodgruppsläsningen", OUTCOME_LABELS[RECORDS]]
    assert rows["YGB.1"][9:11] == ["Bortval med skäl från kodgruppsläsningen", OUTCOME_LABELS[NO_WORK]]
    assert rows["63.B"][9:11] == ["Ingen post och inget bortval med skäl", OUTCOME_LABELS[QUESTION]]
    assert rows["63.B"][11].startswith("Poster (1)") and rows["63.B"][12].startswith("Inget prissatt arbete")
    assert rows["AFB"][10] == "AF: läses inte, bara räknas"
    text = json.dumps(list(rows.values()), ensure_ascii=False)
    assert "Påhittad rubrik" not in text and "Kanalisation" not in text and "allmänt krav" not in text
    over = {r[0].value: r[1].value for r in wb["Översikt"].iter_rows(min_row=5)}
    assert over["Kodgruppsläsning, två läsningar per grupp (se Kodgrupper)"] == (
        "3 grupper: 1 till poster, 1 inget prissatt arbete, 1 frågor"
    )
    assert over["Kodgrupper utan post eller skäl (se Kodgrupper)"] == 2


def test_without_the_pass_no_group_turn_is_sent_and_the_outputs_have_no_trace_of_it(data_root: Path) -> None:
    handler = Handler()
    summary, out = _run(data_root, handler, "k-off", on=False)
    assert not handler.seen and "group_pass" not in summary
    assert "group_pass" not in json.loads((out / "result.json").read_text())
    assert load_kalkyl_config().group_pass is False  # off in config/kalkyl.toml


def test_a_group_both_readings_call_records_but_of_different_kinds_stays_a_question(data_root: Path) -> None:
    other = req("kanalisation", "stated", "st", [cite(A(14), "förläggs dolt i alla bostadsrum")], quantity=1)
    readings = {**READINGS, A(13): [{"records": [PER_ROOM], "set_aside": []}, {"records": [other], "set_aside": []}]}
    summary, out = _run(data_root, Handler(readings), "k-split")
    assert summary["group_pass"]["to_records"] == 0 and summary["group_pass"]["questions"] == 2
    body = json.loads((out / "result.json").read_text())
    assert not any(r["chunk_id"].startswith("g-") for r in body["records"])


def _reading(*records: dict[str, Any]) -> ChunkResult:
    return ChunkResult("g-x", "submitted", records=[(QuantityRecord.model_validate(r), "exact") for r in records])


def test_only_the_records_whose_blocks_both_readings_read_the_same_way_are_kept() -> None:
    chunk = Chunk(id="g-x", doc_id="aaaaaaaaaaaa", block_ids=[A(13), A(14), A(15), A(16)])
    stated_two = {**STATED, "quantity": 2}
    readings = [_reading(PER_ROOM, STATED), _reading(PER_ROOM, stated_two)]  # the stated quantity differs
    result = GroupResult(group=None, chunk=chunk)  # type: ignore[arg-type]
    result.readings = [classify_reading(r, chunk) for r in readings]
    agree(result, readings)
    assert result.outcome == RECORDS and [r.what for r, _ in result.records] == ["kanalisation"]


def test_the_dry_run_bounds_the_pass_by_the_codes_of_the_technical_description(data_root: Path) -> None:
    from anbud.kalkyl.run import plan

    write_coded_prep(paths.dev_tender_dir("co-001", "t-0001") / "prep")
    write_catalog(data_root)
    off = plan("co-001", "t-0001", catalog_name="synthetic")
    on = plan("co-001", "t-0001", catalog_name="synthetic", kalkyl=replace(load_kalkyl_config(), group_pass=True))
    assert "group_pass_calls_max" not in off
    assert on["group_pass_calls_max"] == 14  # 7 technical codes, two readings each
    assert on["cost_usd_est_high"] == round(off["cost_usd_est_high"] + on["group_pass_cost_usd_est_high"], 2)


def test_a_sub_code_the_kept_answer_leaves_uncovered_is_listed_as_read_in_its_group(data_root: Path) -> None:
    readings = {**READINGS, A(13): [{"records": [PER_ROOM], "set_aside": []}] * 2}  # nothing about 63.EB and 63.EC
    summary, out = _run(data_root, Handler(readings), "k-sub")
    assert summary["group_pass"]["to_records"] == 1 and summary["code_groups"]["teknisk"]["rows"] == 3
    wb = openpyxl.load_workbook(out / "kalkyl.xlsx")
    rows = {r[0]: r for r in ([c.value for c in row] for row in wb["Kodgrupper"].iter_rows(min_row=5))}
    assert list(rows) == ["63.B", "63.E", "YGB.1", "63.EB", "63.EC", "AFB"]
    assert rows["63.EB"][9:11] == ["Ingen post och inget bortval med skäl", READ_IN.format(code="63.E")]
