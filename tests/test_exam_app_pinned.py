"""The old engine's app, pinned (audit 2, C9 and E6): a paid exam resolves and exports the app ref once, before
anything is on record, runs both splits on that one commit and records it. Each input's matched settings are checked
before the first run, and again before each old-engine started line; one that fails after its started line stops the
exam. Synthetic store and fake engines from test_exam; no model call, git or subprocess.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_exam import LOCAL_NOW, NOW, FakeEngines, register, sealed_store
from test_exam_breaker import _burned, _names_nothing, _sealed

from anbud.evals import baseline, exam, freeze

COSTS = {"granskning": 8.0, "kalkyl": 1.0, "baseline": 2.0}


def _paid(runner: FakeEngines | None = None) -> list[exam.Report]:
    return exam.run_both(
        "co-002", paid=True, budget_usd=500.0, runner=runner, now=NOW, local_now=LOCAL_NOW, say=lambda _: None
    )


def _baseline_attempts() -> list[tuple[str, str, str]]:
    return [pair for pair in freeze.exam_attempts(_sealed()) if pair[1] == "baseline"]


class ExportFails(FakeEngines):
    """The app checkout broke between the $0 pass and the paid one: no node_modules, a moved ref, a git failure."""

    def _app_export(self) -> Path:
        raise baseline.BaselineError("the app checkout has no node_modules; run `bun install` there first")


def test_an_app_that_cant_be_exported_stops_the_paid_exam_with_nothing_on_record(data_root: Path) -> None:
    """Mutation: dropping run_both's `pin_app` makes it fail (the first split's prep runs, then run_exam's pin stops
    it); dropping both lets the exam start runs."""
    sealed_store(data_root)
    register(data_root)
    runner = ExportFails(COSTS)
    with pytest.raises(exam.ExamError, match="the app can't be exported") as info:
        _paid(runner)
    _names_nothing(str(info.value))
    assert runner.prepped == [] and runner.calls == [] and _sealed() == []


def test_both_splits_run_the_old_engine_on_the_one_commit_pinned_before_either(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the CLI's default (runner=None), each split builds its own Engines. The ref moves on every resolve, as a
    fetch between the splits would move origin/main; a sha resolves to itself. Mutation: passing `app_ref` unchanged
    to the splits instead of the pinned commit makes it fail."""
    sealed_store(data_root)
    register(data_root)
    moving = iter(["a" * 40, "b" * 40, "c" * 40, "d" * 40])
    monkeypatch.setattr(baseline, "resolve_app", lambda app, ref: ref if len(ref) == 40 else next(moving))
    monkeypatch.setattr(baseline, "export_app", lambda app, sha, root=None: data_root / "baseline" / "app" / sha)
    used: list[str | None] = []

    class Pinned(FakeEngines):
        _app_export = exam.Engines._app_export  # the real one, cached per Engines

        def __init__(self, logs: Path, app: Path, app_ref: str, root: Path | None) -> None:
            super().__init__(COSTS)
            self.app, self.app_ref, self.root = app, app_ref, root
            self._app_root: Path | None = None
            self._app_sha: str | None = None

        def baseline(self, vroot, company_id, tender_id, variant, run_id, run_dir, *, dry_run):  # type: ignore[no-untyped-def]
            self._app_export()
            used.append(self._app_sha)
            return self._run("baseline", variant, vroot, company_id, tender_id, run_id)

    monkeypatch.setattr(exam, "Engines", Pinned)
    reports = _paid()
    assert used and set(used) == {"a" * 40}
    assert [r.app_commit for r in reports] == ["a" * 40, "a" * 40]
    for kind in exam.ORDER:  # the exam record names it, in sealed storage
        (record,) = (exam.exam_dir("co-002", kind) / "exams").glob("*.json")
        assert json.loads(record.read_text())["report"]["app_commit"] == "a" * 40


class SettingsMissing(FakeEngines):
    """Each input's matched settings: missing from the start (`after_runs=0`), or once that many runs have ended."""

    def __init__(self, after_runs: int) -> None:
        super().__init__(COSTS)
        self.after_runs = after_runs
        self.ready_checks = 0

    def baseline_ready(self, vroot: Path) -> str | None:
        self.ready_checks += 1
        if len(self.calls) >= self.after_runs:
            return "a run's app or matched settings aren't ready: the default catalog file is missing"
        return None


def test_settings_missing_before_the_first_run_stop_the_split_before_anything_starts(data_root: Path) -> None:
    """Mutation: dropping run_exam's check of each input's settings makes it fail: the exam starts its other runs."""
    sealed_store(data_root)
    register(data_root)
    runner = SettingsMissing(after_runs=0)
    with pytest.raises(exam.ExamError, match="matched settings aren't ready") as info:
        _paid(runner)
    _names_nothing(str(info.value))
    assert runner.calls == [] and _sealed(freeze.STARTED) == []


def test_settings_lost_mid_exam_stop_it_before_the_old_engines_started_line(data_root: Path) -> None:
    """The settings pass the split's first check, then go missing once a run has ended: the exam stops before the
    old engine's started line, so no pair is lost. Mutation: dropping the check before each started line makes it
    fail (the old engine's run starts)."""
    sealed_store(data_root)
    register(data_root)
    runner = SettingsMissing(after_runs=1)
    with pytest.raises(exam.ExamError, match="matched settings aren't ready"):
        _paid(runner)
    assert runner.calls and _baseline_attempts() == [] and _burned() == []


def test_an_app_that_fails_after_its_started_line_stops_the_exam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real `Engines.baseline`: an export or settings failure in the run is that run's failure, not an exception
    out of the exam, and the breaker stops the exam, since every later old-engine run would fail the same way.
    Mutation: dropping `_breaks`' app branch, or the try around the export, makes it fail."""
    engines = exam.Engines(tmp_path / "logs", tmp_path, "main", None)

    def fails() -> Path:
        raise baseline.BaselineError("the app commit has no script")

    monkeypatch.setattr(engines, "_app_export", fails)
    one = engines.baseline(tmp_path, "co-002", "t-0001", "granskning", "r-1", tmp_path / "run", dry_run=False)
    assert (one.ok, one.failures) == (False, {exam.APP_UNREADY: 1})
    why: Any = exam._breaks(one)
    assert why == "a run's app or matched settings failed"
    _names_nothing(why)
