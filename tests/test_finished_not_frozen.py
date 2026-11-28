"""A run that finished, but whose own freeze failed, is a finished pair, never a lost one (audit B6). Both engines exit
FINISHED_NOT_FROZEN (7), not 1: a chmod that failed after the ledger line, a read-only warning, a refused freeze.
The exam counts it as finished, and its sealed freeze does the freezing. A 7 never skips the exam's own checks: the
sealed freeze still refuses a finished run missing an output its engine always writes, which is the realistic cause
of the failed chmod (a file that vanished). The sealed line records the child's exit code. A store failure still
exits 1. Synthetic data and fakes only, $0.

Each test names the one-line mutation it catches; each was made, seen to fail the test, and reverted."""

from __future__ import annotations

import contextlib
import io
import subprocess
from pathlib import Path
from typing import Any

import pytest
from kalkyl_synthetic import add_tile, fake_sdk, good_handler, write_catalog, write_prep
from test_exam import FakeEngines, _freezes, _paid, register, sealed_store
from test_granskning_pipeline import clean_tree, script, tender  # noqa: F401 - fixtures
from test_model_unreachable import _granskning

from anbud import cli, model_client, paths, store
from anbud.evals import exam, freeze
from anbud.evals import split_heldout as sh
from anbud.granskning import command, provenance
from anbud.granskning.command import FINISHED_NOT_FROZEN
from anbud.granskning.pipeline import REQUIRED_OUTPUTS as GRANSKNING_OUTPUTS
from anbud.kalkyl import run as kalkyl_run

NOT_READ_ONLY = ["FileNotFoundError"]  # what `freeze_tree` returns for an entry that vanished before its chmod


class _NotFrozen(FakeEngines):
    """Every run finishes and exits 7. It writes `wrote` of the outputs its engine requires, `a.json` and `b.json`."""

    def __init__(self, wrote: tuple[str, ...]) -> None:
        super().__init__({"granskning": 0.0, "kalkyl": 0.0, "baseline": 0.0})
        self.wrote = wrote

    def _run(self, engine: str, variant: str, vroot: Path, company_id: str, tender_id: str, run_id: str) -> Any:
        one = super()._run(engine, variant, vroot, company_id, tender_id, run_id)
        if engine == "baseline":
            return one
        for name in self.wrote:
            (one.outputs_dir / name).write_text("{}")
        one.exit_code, one.required = FINISHED_NOT_FROZEN, ("a.json", "b.json")
        return one


def test_a_finished_run_whose_own_freeze_failed_is_sealed_finished_with_its_exit_code(data_root: Path) -> None:
    """Mutation: `"exit_code": one.exit_code,` -> deleted in `run_exam`'s sealed line: a finished-but-not-frozen pair
    can't be counted afterwards."""
    sealed_store(data_root)
    register(data_root)
    _paid(_NotFrozen(("a.json", "b.json")), [])
    lines = [r for r in _freezes() if r["exam"]["engine"] in ("granskning", "kalkyl")]
    assert lines and {r["status"] for r in lines} == {freeze.FINISHED}
    assert {r["exam"]["exit_code"] for r in lines} == {FINISHED_NOT_FROZEN}


def test_a_seven_whose_output_vanished_is_refused_by_the_sealed_freeze_and_never_finished(data_root: Path) -> None:
    """Mutation: `required=one.required,` -> deleted in `run_exam`'s sealed freeze: the run that lost an output is
    sealed as a finished prediction."""
    sealed_store(data_root)
    register(data_root)
    report = _paid(_NotFrozen(("a.json",)), [])
    assert not [r for r in _freezes() if r["exam"]["engine"] in ("granskning", "kalkyl")]
    assert report.runs_failed["granskning"] == report.runs_failed["kalkyl"] > 0
    assert report.runs_ok.get("granskning", 0) == report.runs_ok.get("kalkyl", 0) == 0
    ended = report.combined["paid"]["ended"]
    assert ended[exam.NEVER_FROZEN]["granskning"] == ended[exam.NEVER_FROZEN]["kalkyl"] > 0


def test_anbud_run_exits_7_when_its_outputs_cant_be_made_read_only_and_the_exam_reads_finished(
    tender: Path,  # noqa: F811 - the fixture
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: the last `return FINISHED_NOT_FROZEN` in `freeze_after_run` -> `return 1`: a finished, ledger-frozen
    run is read as FAILED, and the exam seals a complete prediction as failed."""
    monkeypatch.setattr(command, "freeze_tree", lambda path: NOT_READ_ONLY)
    one = _granskning(data_root, monkeypatch, script, "t-0001-b6")
    assert (one.exit_code, one.status) == (FINISHED_NOT_FROZEN, freeze.FINISHED)
    assert freeze.frozen_line("co-001", "t-0001-b6") == 1  # its ledger line was written before the chmod


def test_anbud_run_exits_7_when_its_own_freeze_is_refused(
    tender: Path,  # noqa: F811 - the fixture
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: `return FINISHED_NOT_FROZEN` after `except FreezeError` in `freeze_after_run` -> `return 1`."""

    def refused(*args: Any, **kwargs: Any) -> Any:
        raise freeze.FreezeError("the ledger is locked")

    monkeypatch.setattr(freeze, "freeze_run", refused)
    one = _granskning(data_root, monkeypatch, script, "t-0001-b6r")
    assert (one.exit_code, one.status) == (FINISHED_NOT_FROZEN, freeze.FINISHED)
    assert freeze.frozen_line("co-001", "t-0001-b6r") is None


def test_every_output_the_exam_requires_is_what_a_finished_granskning_run_writes(
    tender: Path,  # noqa: F811 - the fixture
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: `"flags.json",` -> `"flag.json",` in granskning's REQUIRED_OUTPUTS: every finished exam pair would be
    refused by the sealed freeze."""
    one = _granskning(data_root, monkeypatch, script, "t-0001-b6o")
    assert one.exit_code == 0 and one.required == GRANSKNING_OUTPUTS
    assert all((one.outputs_dir / name).is_file() for name in one.required)


def _kalkyl(data_root: Path, monkeypatch: pytest.MonkeyPatch, run_id: str) -> tuple[int, str]:
    tender_dir = paths.dev_tender_dir("co-001", "t-0001")
    write_prep(tender_dir / "prep")
    add_tile(tender_dir / "prep", store.RunStore("co-001"))
    write_catalog(data_root)
    sh.append_events(paths.ledger_path("co-001"), [sh._assign("t-0001", sh.DEV, "test", ())])
    sdk = fake_sdk(good_handler)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(model_client, "keychain_key", lambda **_: "sk-test")
    monkeypatch.setattr(model_client, "make_sdk_client", lambda config, key: sdk)
    monkeypatch.setattr(provenance, "git_state", lambda root=None: provenance.GitState("a" * 40, False, None))
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["kalkyl", "co-001", "t-0001", "--catalog", "synthetic", "--run-id", run_id])
    return code, out.getvalue()


def test_anbud_kalkyl_exits_7_when_its_finished_calc_cant_be_made_read_only(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: `return FINISHED_NOT_FROZEN` after `freeze_failures` in `_kalkyl` -> `return 1`: a complete calc
    that printed `frozen: True` is read as FAILED by the exam."""
    monkeypatch.setattr(kalkyl_run, "freeze_tree", lambda path: NOT_READ_ONLY)
    code, out = _kalkyl(data_root, monkeypatch, "kalkyl-b6")
    assert code == FINISHED_NOT_FROZEN and "frozen: True" in out and "freeze_failures: 2" in out
    calc = paths.dev_tender_dir("co-001", "t-0001") / "kalkyl" / "kalkyl-b6"
    # What the exam's sealed freeze will require of it is there.
    assert all((calc / name).is_file() for name in kalkyl_run.REQUIRED_OUTPUTS)


def test_a_store_failure_still_exits_1_and_fails(
    tender: Path,  # noqa: F811 - the fixture
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boundary: a run whose store writes failed never reaches the freeze, and stays exit 1 and FAILED."""

    def broken(self: store.RunStore, *args: Any, **kwargs: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(store.RunStore, "append_event", broken)  # every call's event: a full disk
    one = _granskning(data_root, monkeypatch, script, "t-0001-b6s")
    assert (one.exit_code, one.status) == (1, freeze.FAILED)
    assert freeze.frozen_line("co-001", "t-0001-b6s") is None


@pytest.mark.parametrize(
    ("engine", "stdout", "status"),
    [
        ("granskning", "", freeze.FINISHED),
        ("kalkyl", "frozen: True\nhalted: 0", freeze.FINISHED),
        ("kalkyl", "frozen: False\nhalted: 0", freeze.FAILED),  # an incomplete calc is never finished, 7 or not
    ],
)
def test_the_exam_reads_7_as_finished_only_for_a_complete_run(
    engine: str, stdout: str, status: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: `code in FINISHED,` -> `code == 0,` in `Engines.granskning`: a 7 is read as FAILED."""
    runner = exam.Engines(tmp_path / "logs", tmp_path, "HEAD", tmp_path)
    done = subprocess.CompletedProcess([], FINISHED_NOT_FROZEN, f"cost_usd: 1.0\n{stdout}", "")
    monkeypatch.setattr(runner, "_anbud", lambda vroot, args, log: done)
    one = getattr(runner, engine)(tmp_path, "co-002", "t-0002", "r1")
    assert one.status == status and one.required
