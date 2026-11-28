"""Every count the exam prints is one total over both splits, printed once per invocation, after both splits.

The held-out split is one tender, so a count of its own (quarantine, prep, dry runs, sizes, the estimate) is that
tender's. And a total printed after the prospective split alone holds the held-out tender's figures from its last
pass, so its difference from the total printed after both would be that tender's change: after a code change between
two invocations, say. Each test names the one-line mutation that makes it fail. Synthetic store and fake engines from
test_exam; no model call.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
from test_exam import LOCAL_NOW, NOW, FakeEngines, sealed_store

from anbud import cli
from anbud.evals import exam

COSTS = {"granskning": 8.0, "kalkyl": 1.0, "baseline": 2.0}
HELD = "t-0001"  # sealed_store's held-out tender, the split's only one

# How the held-out tender's second pass can come out, each changing its prep, dry runs, sizes or estimate.
HELD_OUT = {
    "as before": {},
    "stopped in prep": {"stops": {HELD: "read_error"}},
    "larger": {"tokens": {HELD: 900_000}},
}


def _invocation(runner: FakeEngines, at: int) -> list[str]:
    lines: list[str] = []
    exam.run_both("co-002", runner=runner, now=NOW + timedelta(minutes=at), local_now=LOCAL_NOW, say=lines.append)
    return lines


def _split(lines: list[str]) -> tuple[list[str], list[str]]:
    """The lines before the invocation's total over both splits, and the total."""
    first = next(i for i, x in enumerate(lines) if x.startswith(("both splits", "dry runs, both splits")))
    return lines[:first], lines[first:]


def test_no_line_before_the_one_total_depends_on_the_single_held_out_tender(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three exams with the same first invocation, then a second in which the held-out tender comes out as before,
    stops in prep, or is larger. Everything the second prints before its total is the same in all three, the total
    is printed once, after the held-out split, and no line of it names a split. Mutations: `say_total=True` in
    `run_both`, a split's own `prep:` line in `_say`, or the split counts back in the sizes line, each make it fail."""
    firsts: dict[str, list[str]] = {}
    seconds: dict[str, list[str]] = {}
    for name, held in HELD_OUT.items():
        root = tmp_path / name.replace(" ", "-")
        root.mkdir()
        monkeypatch.setenv("ANBUD_DATA_ROOT", str(root))
        sealed_store(root)
        firsts[name] = _invocation(FakeEngines(COSTS), 0)
        seconds[name] = _invocation(FakeEngines(COSTS, **held), 1)

    assert len({tuple(lines) for lines in firsts.values()}) == 1
    befores = {name: _split(lines)[0] for name, lines in seconds.items()}
    totals = {name: _split(lines)[1] for name, lines in seconds.items()}
    assert len({tuple(lines) for lines in befores.values()}) == 1, befores  # nothing before the total depends on it
    before = befores["as before"]
    assert [x for x in before if x.startswith("anbud eval ")] == [
        "anbud eval prospective: 3 tenders; counts are over both splits",
        "anbud eval heldout: 1 tenders; counts are over both splits",  # the last split's header comes before the total
    ]
    assert not [
        x for x in before if re.search(r"\d", x) and not x.startswith(("anbud eval ", "deadlines:", "warning:"))
    ]
    for lines in totals.values():
        assert [x for x in lines if x.startswith("both splits, ") and " tenders: answer keys" in x] == [lines[0]]
        assert not [x for x in lines if "heldout" in x or "held-out" in x or "prospective 3" in x], lines

    # The variants do change the total: the held-out tender is one of its counts, and nothing else shows it.
    assert totals["stopped in prep"] != totals["as before"] != totals["larger"]
    assert totals["as before"][0].endswith("; prep: 4 prepared, 0 stopped")
    assert totals["stopped in prep"][0].endswith("; prep: 3 prepared, 1 stopped (read_error 1)")
    assert [x for x in totals["stopped in prep"] if x.startswith("both splits, one total over 3 prepared tenders: ")]
    # A tender that stopped this pass counts no dry run, not even the ones its last pass made.
    assert "dry runs, both splits: baseline 7 of 7 reached the first model call" in totals["as before"]
    assert "dry runs, both splits: baseline 5 of 5 reached the first model call" in totals["stopped in prep"]


def test_fewer_than_two_tenders_examined_prints_no_count() -> None:
    lines: list[str] = []
    exam._say_combined({"tenders": 1, "records": exam._record_counts([{"prep": exam.PREPARED, "held": {"a": 2}}])},
                       lines.append)  # fmt: skip
    assert lines == ["both splits: fewer than 2 tenders examined, so no counts"]


@pytest.mark.parametrize("paid", [[], ["--paid", "--budget", "10"]], ids=["at $0", "paid"])
@pytest.mark.parametrize("kind", ["prospective", "heldout"])
def test_a_split_alone_is_refused_at_0_and_paid(
    kind: str, paid: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A split's pass alone would print a total holding the other split's figures from its last pass. Mutation:
    `if args.paid and args.kind != "exam":` for the CLI's refusal of a split alone makes the $0 cases fail."""
    monkeypatch.setattr(cli, "held_out_refusal", lambda: None)
    monkeypatch.setattr(exam, "run_both", lambda *a, **kw: pytest.fail("a split alone ran"))
    assert cli.main(["eval", kind, "co-002", *paid]) == cli.REFUSED
    assert "the exam runs both splits in one invocation, at $0 or paid" in capsys.readouterr().err


def _raises(exc: Exception) -> Callable[..., list[exam.Report]]:
    def run(*_: object, **__: object) -> list[exam.Report]:
        raise exc

    return run


def test_the_cli_shows_the_exam_s_own_errors_and_of_any_other_only_its_type(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An error raised while a tender is read may quote it: a library's ValueError often does (`int('...')`). The
    `anbud eval` line it would be on is one `scripts/exam-run.sh status` shows. Mutation: `except (exam.ExamError,
    ValueError) as exc:` in the CLI makes it fail."""
    monkeypatch.setattr(cli, "held_out_refusal", lambda: None)
    monkeypatch.setattr(exam, "run_both", _raises(ValueError("invalid literal for int(): 'a line of a document'")))
    assert cli.main(["eval", "exam", "co-002"]) == cli.REFUSED
    err = capsys.readouterr().err
    assert "stopped: ValueError" in err and "a line of a document" not in err
    monkeypatch.setattr(exam, "run_both", _raises(exam.ExamError("a paid exam needs --budget, in USD")))
    assert cli.main(["eval", "exam", "co-002"]) == cli.REFUSED
    assert "stopped: a paid exam needs --budget, in USD" in capsys.readouterr().err


def test_a_bad_company_id_is_the_exam_s_own_error_before_any_tender_is_read(data_root: Path) -> None:
    sealed_store(data_root)
    with pytest.raises(exam.ExamError, match="the exam can't start: company ids look like co-001"):
        exam.run_exam("co-2", "prospective", runner=FakeEngines(COSTS), now=NOW, say=lambda _: None)
    assert not exam.exam_dir("co-002", "prospective").exists()
