"""The sealed ledger's head (audit 2, a2-14 and E9). The chain checks each line against the one before, so dropping the
newest lines at a line boundary leaves a chain that verifies, and the dropped pairs would run again. Two anchors:

- `<ledger>.head` beside every ledger, written after each append: every read refuses a ledger that lost lines.
- Each exam record names the head its pass left: each pass first refuses a sealed ledger that lost any of them, even
  with the head file lost too.

Synthetic store and fake engines from test_exam; no model call.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_exam import LOCAL_NOW, NOW, FakeEngines, register, sealed_store

from anbud.evals import exam, freeze

COSTS = {"granskning": 8.0, "kalkyl": 1.0, "baseline": 2.0}
LATER = datetime(2026, 12, 1, 9, 30, tzinfo=UTC)


class Recording(FakeEngines):
    def __init__(self) -> None:
        super().__init__(COSTS)
        self.ran: list[tuple[str, str]] = []

    def _run(self, engine, variant, vroot, company_id, tender_id, run_id):  # type: ignore[no-untyped-def]
        self.ran.append((tender_id, engine))
        return super()._run(engine, variant, vroot, company_id, tender_id, run_id)


def _pass(runner: FakeEngines, now: datetime, engine: str = "granskning") -> exam.Report:
    return exam.run_exam(
        "co-002", "prospective", paid=True, budget_usd=500.0, runner=runner, now=now, local_now=LOCAL_NOW,
        say=lambda _: None, engines=(engine,),
    )  # fmt: skip


def _lines() -> int:
    return len(_ledger().read_bytes().splitlines())


def _ledger() -> Path:
    return freeze.ledger_file("co-002", sealed=True)


def _keep(n: int) -> None:
    """The ledger cut to its first n lines at a line boundary: a chain that still verifies."""
    ledger = _ledger()
    lines = ledger.read_bytes().split(b"\n")[:-1]
    ledger.chmod(0o600)
    ledger.write_bytes(b"".join(x + b"\n" for x in lines[:n]))


def _one_pass(data_root: Path) -> int:
    sealed_store(data_root)
    register(data_root)
    _pass(FakeEngines(COSTS), NOW)
    return len(_ledger().read_bytes().splitlines())


def test_every_read_refuses_a_ledger_cut_below_its_head(data_root: Path) -> None:
    """Mutation: an early return in `freeze._check_head` makes it fail."""
    n = _one_pass(data_root)
    head = json.loads(freeze.head_file(_ledger()).read_text())
    assert head["lines"] == n
    _keep(n - 2)
    with pytest.raises(freeze.FreezeError, match="lost lines: it has .* lines and its head was line"):
        freeze.records(_ledger())


def test_a_pass_refuses_a_ledger_that_lost_lines_with_its_head_file_too(data_root: Path) -> None:
    """Both lost (a restore of the ledger's folder): the exam record still names the head, and the pass refuses
    before it runs anything. Mutation: dropping `_check_ledger_heads` from run_exam makes it fail."""
    n = _one_pass(data_root)
    _keep(n - 2)
    freeze.head_file(_ledger()).unlink()
    again = Recording()
    with pytest.raises(exam.ExamError, match="the sealed ledger: the ledger has lost lines since a record named"):
        _pass(again, LATER)
    assert again.ran == [] and again.prepped == []


def test_the_exam_record_names_the_head_its_pass_left(data_root: Path) -> None:
    """Mutation: `_sealed_head` at the start of the pass instead of the end makes it fail."""
    n = _one_pass(data_root)
    (record,) = (exam.exam_dir("co-002", "prospective") / "exams").glob("*.json")
    assert json.loads(record.read_text())["ledger_head"] == freeze.head_of(_ledger()) and n > 0
    assert freeze.head_of(_ledger())["lines"] == n


def test_a_last_line_replaced_by_another_chained_line_is_refused(data_root: Path) -> None:
    """Same length, valid chain, a different last line (a started line's group or deadline state rewritten and
    re-chained): the head's hash says it isn't the line it had."""
    n = _one_pass(data_root)
    ledger = _ledger()
    lines = ledger.read_bytes().split(b"\n")[:-1]
    last = json.loads(lines[-1])
    last["note"] = "rewritten"
    ledger.chmod(0o600)
    ledger.write_bytes(b"".join(x + b"\n" for x in lines[:-1]) + json.dumps(last).encode() + b"\n")
    assert len(ledger.read_bytes().splitlines()) == n
    with pytest.raises(freeze.FreezeError, match=f"its line {n} isn't the head line it had"):
        freeze.records(ledger)


def test_a_deleted_ledger_with_a_head_is_refused(data_root: Path) -> None:
    _one_pass(data_root)
    _ledger().unlink()
    with pytest.raises(freeze.FreezeError, match="lost lines: it has 0 lines"):
        freeze.records(_ledger())


def test_a_head_one_append_behind_reads_and_the_next_append_catches_up(data_root: Path) -> None:
    """A crash between the line's fsync and the head's rename leaves the head one append behind: still a prefix, so
    it reads; the next append writes the head again. Mutation: `before + len(added)` as `before` makes it fail."""
    n = _one_pass(data_root)
    lines = _ledger().read_bytes().split(b"\n")[:-1]
    freeze._write_head(_ledger(), n - 1, lines[n - 2])
    assert len(freeze.records(_ledger())) == n
    _pass(FakeEngines(COSTS), LATER, "kalkyl")  # more lines on the sealed ledger
    assert _lines() > n and json.loads(freeze.head_file(_ledger()).read_text())["lines"] == _lines()


def test_a_ledger_from_before_heads_reads_and_gets_one_on_its_next_append(data_root: Path) -> None:
    n = _one_pass(data_root)
    freeze.head_file(_ledger()).unlink()
    assert len(freeze.records(_ledger())) == n
    _pass(FakeEngines(COSTS), LATER, "kalkyl")
    assert _lines() > n and json.loads(freeze.head_file(_ledger()).read_text())["lines"] == _lines()


def test_an_unreadable_head_is_refused(data_root: Path) -> None:
    _one_pass(data_root)
    freeze.head_file(_ledger()).write_text("{not json")
    with pytest.raises(freeze.FreezeError, match="its head record is unreadable"):
        freeze.records(_ledger())


def test_the_audit_case_a_truncated_ledger_runs_no_pair_again(data_root: Path) -> None:
    """a2-14 as the audit wrote it: drop the last pair's started and freeze lines; the next pass may refuse, but must
    not run that pair again."""
    n = _one_pass(data_root)
    last = json.loads(_ledger().read_bytes().splitlines()[-1])
    assert last["kind"] == "freeze"
    _keep(n - 2)
    again = Recording()
    with pytest.raises(exam.ExamError, match="the sealed ledger"):
        _pass(again, LATER)
    assert (last["tender"].split("/")[1], "granskning") not in again.ran
