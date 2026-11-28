"""The paid exam's gate and circuit breaker.

An attempt is never run again (scoring plan v3.1), so a run sent into a broken environment loses its pair for good,
and without a stop every pair after it would too.

- The gate (`Engines.gate`) runs at $0 before the paid exam reads either split, and again before each started line.
  It checks that the checkout is clean and still on the commit the exam started at, that the key reads, and that the
  API answers a token count (`check_api`).
- The breaker (`_breaks`) runs after each run. It stops the exam, once the run is on record, after any of these:
  - a Granskning or Kalkyl run refused before any call (exit 3);
  - a Granskning or Kalkyl run with no lens or extraction call answered (exit 6, `MODEL_UNREACHABLE`);
  - an old-engine run with none of its calls answered;
  - a run whose calls failed for a reason every later run shares (`ENVIRONMENTAL`).

Pairs not started stay owed: the same command, once the cause is fixed, runs only those. Each test names a mutation
that makes it fail. Synthetic store, fake engines from test_exam and temporary git checkouts: no model call, and no
key (conftest's `_never_the_real_key`).
"""

from __future__ import annotations

import dataclasses
import json
import re
import subprocess
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import pytest
from test_exam import LOCAL_NOW, NOW, FakeEngines, register, sealed_store
from test_model_client import REQUEST, _status_error

from anbud import model_client, paths, schema_check
from anbud.config import load_config
from anbud.evals import baseline, exam, freeze
from anbud.granskning import provenance

COSTS = {"granskning": 8.0, "kalkyl": 1.0, "baseline": 2.0}
FIRST = "t-0002"  # the first prospective tender: the exam's first runs are its Granskning, Kalkyl, then old engine


def _paid(runner: exam.Engines, at: int = 0, engines: tuple[str, ...] = exam.ENGINES) -> list[str]:
    lines: list[str] = []
    exam.run_both(
        "co-002",
        paid=True,
        budget_usd=500.0,
        engines=engines,
        runner=runner,
        now=NOW + timedelta(minutes=at),
        local_now=LOCAL_NOW,
        say=lines.append,
    )
    return lines


def _sealed(kind: str | None = None) -> list[dict[str, Any]]:
    path = freeze.ledger_file("co-002", sealed=True)
    lines = path.read_text().splitlines() if path.exists() else []
    return [r for r in map(json.loads, lines) if kind is None or r.get("kind") == kind]


def _burned() -> list[dict[str, Any]]:
    """Pairs on record as started with no freeze: an attempt spent with nothing to score."""
    frozen = {r["run_id"] for r in _sealed("freeze")}
    return [r for r in _sealed(freeze.STARTED) if r["run_id"] not in frozen]


def _names_nothing(message: str) -> None:
    """A stop says why, and nothing of what was run: no figure, tender, engine or split."""
    assert not re.search(r"\$\d|t-\d{4}|granskning|kalkyl|baseline|heldout|held-out|prospective", message), message


# The gate.


@pytest.mark.parametrize("kind", ["key_missing", "auth", "connection", "timeout"])
def test_a_broken_environment_found_before_the_exam_starts_writes_nothing(data_root: Path, kind: str) -> None:
    """Mutation: `if False:` for run_both's gate makes it fail (the gate before the first started line still stops
    it, but only after both splits' prep)."""
    sealed_store(data_root)
    register(data_root)
    runner = FakeEngines(COSTS, api=[kind])
    with pytest.raises(exam.ExamError, match=rf"the API check failed \({kind}\)") as info:
        _paid(runner)
    _names_nothing(str(info.value))
    assert runner.prepped == [] and runner.calls == [] and _sealed() == []


def test_the_free_pass_needs_no_key(data_root: Path) -> None:
    """Mutation: `if True:` for run_both's `if paid:` gate makes it fail."""
    sealed_store(data_root)
    register(data_root)
    runner = FakeEngines(COSTS, api=["key_missing"] * 50)
    lines: list[str] = []
    exam.run_both("co-002", runner=runner, now=NOW, local_now=LOCAL_NOW, say=lines.append)
    assert runner.api_checks == 0 and any(x.startswith("dry runs, both splits") for x in lines)


def test_an_environment_that_breaks_mid_exam_stops_before_the_next_started_line_and_loses_no_more_pairs(
    data_root: Path,
) -> None:
    """The gate passes three times (the exam's start, then before each of two runs), then fails. Nothing more starts,
    and once it passes again the same command runs only the pairs left and completes the exam. Mutation: `if False:`
    for the gate before each started line makes it fail."""
    sealed_store(data_root)
    register(data_root)
    broken = FakeEngines(COSTS, api=[None, None, None, "connection"])
    with pytest.raises(exam.ExamError, match=r"the API check failed \(connection\)"):
        _paid(broken)
    assert len(broken.calls) == 2 and len(_sealed(freeze.STARTED)) == len(_sealed("freeze")) == 2

    fixed = FakeEngines(COSTS)
    lines = _paid(fixed, at=1)
    total = exam.paid_summary("co-002")
    assert lines[-1].startswith("paid exam: complete. One total over both splits")
    assert total["attempts"] == total["pairs"] == len(_sealed(freeze.STARTED)) == 2 + len(fixed.calls)
    assert fixed.api_checks == len(fixed.calls) + 1  # once at the start, then before each started line


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clean git checkout the exam and its children run from."""
    root = tmp_path / "harness"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "a.py").write_text("x = 1\n")
    _commit(root)
    monkeypatch.setattr(provenance, "repo_root", lambda: root)
    assert provenance.paid_refusal(provenance.git_state()) is None
    return root


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _commit(root: Path) -> None:
    _git(root, "add", ".")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "c")


def test_the_gate_refuses_a_dirty_or_moved_checkout_before_it_checks_the_api(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutations: `if False:` for the dirty-tree branch of `Engines.gate`, or for its moved-commit branch, each make
    it fail."""
    engines = exam.Engines(Path("logs"), Path("."), "main", None)
    checks: list[int] = []
    monkeypatch.setattr(engines, "api_check", lambda: checks.append(1))
    launch = engines.launch_sha()
    assert launch is not None and engines.gate(launch) is None and checks == [1]

    (checkout / ".DS_Store").write_bytes(b"\0\0\0\1Bud1")
    assert engines.gate(launch) == "the checkout isn't a clean git checkout" and checks == [1]
    (checkout / ".DS_Store").unlink()

    (checkout / "a.py").write_text("x = 2\n")
    _commit(checkout)
    assert engines.gate(launch) == "the checkout moved off the commit the exam started at" and checks == [1]
    assert engines.gate(engines.launch_sha()) is None

    monkeypatch.setattr(engines, "api_check", lambda: "auth")
    assert engines.gate(engines.launch_sha()) == "the API check failed (auth)"


class ChildrenCheckTheTree(FakeEngines):
    """The real gate on a real checkout. Granskning and Kalkyl children refuse a dirty tree with exit 3 and no record,
    as `anbud run` and `anbud kalkyl` do. The first child drops a .DS_Store into the checkout after it runs, as Finder
    browsing the checkout would. (Audit 2, repro a2_7.)"""

    gate = exam.Engines.gate
    launch_sha = exam.Engines.launch_sha

    def __init__(self, checkout: Path) -> None:
        super().__init__(COSTS)
        self.checkout = checkout
        self.children = 0
        self.refused = 0

    def _child(self, engine: str, vroot: Path, company_id: str, tender_id: str, run_id: str) -> exam.EngineRun:
        self.children += 1
        if provenance.paid_refusal(provenance.git_state()) is not None:
            self.refused += 1
            one = exam.EngineRun(engine, engine, run_id, exam.PAID_REFUSED, False, None)
        else:
            one = self._run(engine, engine, vroot, company_id, tender_id, run_id)
        if self.children == 1:
            (self.checkout / ".DS_Store").write_bytes(b"\0\0\0\1Bud1")
        return one

    def granskning(self, vroot: Path, company_id: str, tender_id: str, run_id: str) -> exam.EngineRun:
        return self._child("granskning", vroot, company_id, tender_id, run_id)

    def kalkyl(self, vroot: Path, company_id: str, tender_id: str, run_id: str) -> exam.EngineRun:
        return self._child("kalkyl", vroot, company_id, tender_id, run_id)


def test_a_file_appearing_mid_exam_stops_it_before_the_next_started_line(data_root: Path, checkout: Path) -> None:
    """Mutation: `if False:` for the dirty-tree branch of `Engines.gate` makes it fail: every later Granskning and
    Kalkyl pair is started, refused by its child and never frozen."""
    sealed_store(data_root)
    register(data_root)
    runner = ChildrenCheckTheTree(checkout)
    with pytest.raises(exam.ExamError, match="the checkout isn't a clean git checkout"):
        _paid(runner)
    assert runner.children == 1 and runner.refused == 0 and _burned() == []

    (checkout / ".DS_Store").unlink()
    again = ChildrenCheckTheTree(checkout)
    again.children = 1  # nothing more is dropped
    lines = _paid(again, at=1)
    assert lines[-1].startswith("paid exam: complete. One total over both splits") and _burned() == []


# The breaker.


@pytest.mark.parametrize(
    ("engine", "kind"),
    [("granskning", "spend_cap"), ("granskning", "key_missing"), ("kalkyl", "auth"), ("baseline", "spend_cap")],
)
def test_a_run_whose_calls_the_api_refused_for_good_stops_the_exam_after_its_freeze(
    data_root: Path, engine: str, kind: str
) -> None:
    """A token count is free, so the gate can pass while the credit balance can't pay for a call: the run's own
    failed calls say so. The run is frozen as it ended, and nothing after it starts. Mutation: `if False:` for the
    stop after the freeze makes it fail."""
    sealed_store(data_root)
    register(data_root)
    runner = FakeEngines(COSTS, failures={(FIRST, engine): {kind: 3, "server": 1}})
    with pytest.raises(exam.ExamError, match=rf"the API refused a run's calls \({kind}\)") as info:
        _paid(runner)
    _names_nothing(str(info.value))
    ran = ["granskning", "kalkyl", "baseline"][: ["granskning", "kalkyl", "baseline"].index(engine) + 1]
    assert [e for e, _ in runner.calls] == ran
    assert len(_sealed(freeze.STARTED)) == len(_sealed("freeze")) == len(ran)  # the refused run is on record, frozen


@pytest.mark.parametrize(
    ("engine", "code", "why"),
    [
        ("granskning", exam.PAID_REFUSED, "a run was refused before any call"),
        ("granskning", exam.MODEL_UNREACHABLE, "a run reached no model"),
        ("kalkyl", exam.PAID_REFUSED, "a run was refused before any call"),
        ("kalkyl", exam.MODEL_UNREACHABLE, "a run reached no model"),
    ],
)
def test_a_run_refused_or_reaching_no_model_stops_the_exam(data_root: Path, engine: str, code: int, why: str) -> None:
    """Mutations: `False` for either exit-code condition in `_breaks` makes the matching cases fail."""
    sealed_store(data_root)
    register(data_root)
    runner = FakeEngines(COSTS, codes={(FIRST, engine): code})
    with pytest.raises(exam.ExamError, match=re.escape(why)) as info:
        _paid(runner)
    _names_nothing(str(info.value))
    assert [e for e, _ in runner.calls] == (["granskning"] if engine == "granskning" else ["granskning", "kalkyl"])


@pytest.mark.parametrize("engine", ["granskning", "kalkyl"])
def test_a_run_that_finished_but_couldnt_freeze_itself_doesnt_stop_the_exam(data_root: Path, engine: str) -> None:
    """Exit 7 (the build lane's FINISHED_NOT_FROZEN, audit B6): the run finished and only its own freeze step failed.
    That says nothing about the environment, so the exam goes on. Mutation: adding `or one.exit_code == 7` to the
    exit-3 condition in `_breaks` makes it fail."""
    from anbud.granskning import command

    sealed_store(data_root)
    register(data_root)
    code = getattr(command, "FINISHED_NOT_FROZEN", 7)
    lines = _paid(FakeEngines(COSTS, codes={(FIRST, engine): code}))
    assert lines[-1].startswith("paid exam: complete. One total over both splits")


def test_model_unreachable_is_the_engines_exit_6() -> None:
    from anbud.granskning import command

    assert exam.MODEL_UNREACHABLE == command.MODEL_UNREACHABLE == 6


def test_an_outage_that_passed_doesn_t_stop_the_exam(data_root: Path) -> None:
    """A connection or server error after its retries costs that run a call; when the next gate passes, the exam goes
    on. Mutation: adding "connection" to `ENVIRONMENTAL` makes it fail."""
    sealed_store(data_root)
    register(data_root)
    lines = _paid(FakeEngines(COSTS, failures={(FIRST, "granskning"): {"connection": 1, "server": 2, "timeout": 1}}))
    assert lines[-1].startswith("paid exam: complete. One total over both splits")


class CreditRunsOut(FakeEngines):
    """The credit balance runs out after `after` runs. The free token count still answers, so the gate passes, but
    every later run reaches no model: its calls are refused as spend_cap, as the model client counts a credit 400."""

    def __init__(self, after: int) -> None:
        super().__init__(COSTS)
        self.after = after

    def _run(self, engine, variant, vroot, company_id, tender_id, run_id):  # type: ignore[no-untyped-def]
        one = super()._run(engine, variant, vroot, company_id, tender_id, run_id)
        if len(self.calls) <= self.after:
            return one
        code = exam.MODEL_UNREACHABLE if engine != "baseline" else 1
        return dataclasses.replace(one, exit_code=code, ok=False, failures={"spend_cap": 2})


@pytest.mark.parametrize("after", [0, 2, 4])
def test_one_environmental_fault_burns_at_most_the_pair_in_flight(data_root: Path, after: int) -> None:
    """Mutation: `breaks = None` in the paid loop makes it fail: every pair after the fault is spent on it."""
    sealed_store(data_root)
    register(data_root)
    runner = CreditRunsOut(after)
    with pytest.raises(exam.ExamError, match="stopped before the next run"):
        _paid(runner)
    lost = [r for r in _sealed("freeze") if r.get("status") != freeze.FINISHED]
    assert len(runner.calls) == after + 1 and len(lost) == 1  # the one run in flight when the credit ran out

    fixed = FakeEngines(COSTS)
    lines = _paid(fixed, at=1)
    total = exam.paid_summary("co-002")
    assert lines[-1].startswith("paid exam: complete. One total over both splits")
    assert total["attempts"] == total["pairs"] == after + 1 + len(fixed.calls)


class EnvRefused(FakeEngines):
    """The real `Engines.granskning` and `Engines.kalkyl` over children that refuse before any call (exit 3, no run
    record), as `anbud run` does when the key can't be read or the tree is dirty. (Audit 2, repro a2_2.)"""

    def __init__(self) -> None:
        super().__init__(COSTS)
        self.children: list[str] = []

    def _anbud(self, vroot, args, log_name):  # type: ignore[no-untyped-def]
        self.children.append(log_name)
        out = "budget spent $0.00 of $20.00\n" if args[0] == "run" else ""
        return subprocess.CompletedProcess(args, exam.PAID_REFUSED, out, "anbud run: the model key couldn't be read\n")

    def granskning(self, vroot, company_id, tender_id, run_id):  # type: ignore[no-untyped-def]
        return exam.Engines.granskning(self, vroot, company_id, tender_id, run_id)

    def kalkyl(self, vroot, company_id, tender_id, run_id):  # type: ignore[no-untyped-def]
        return exam.Engines.kalkyl(self, vroot, company_id, tender_id, run_id)


def test_a_child_refused_before_any_call_spends_one_pair_and_the_rest_stay_owed(data_root: Path) -> None:
    """Without the breaker every sealed pair of both splits was started and spent on the same $0 refusal. Mutation:
    `False` for the exit-3 condition in `_breaks` makes it fail."""
    sealed_store(data_root)
    register(data_root)
    engines = ("granskning", "kalkyl")
    with pytest.raises(exam.ExamError, match="a run was refused before any call"):
        _paid(EnvRefused(), engines=engines)
    assert len(_sealed(freeze.STARTED)) == len(_burned()) == 1

    again = FakeEngines(COSTS)
    _paid(again, at=1, engines=engines)
    assert len(again.calls) == 7 and len(_sealed(freeze.STARTED)) == 8  # 4 tenders x 2 engines: one lost, none twice


class KeyStopsReading(FakeEngines):
    """The key reads at the gate, then not for the old engine's run: `Engines.baseline` returns a run that left
    nothing to freeze and names the key (audit 2, repro a2_6)."""

    def baseline(self, vroot, company_id, tender_id, variant, run_id, run_dir, *, dry_run):  # type: ignore[no-untyped-def]
        self.calls.append(("baseline", variant))
        return exam.EngineRun("baseline", variant, run_id, None, False, 0.0, failures={"key_missing": 1})


def test_a_run_that_left_nothing_to_freeze_still_stops_the_exam(data_root: Path) -> None:
    """Mutation: `if False:` for the stop in the nothing-to-freeze branch makes it fail."""
    sealed_store(data_root)
    register(data_root)
    runner = KeyStopsReading(COSTS)
    with pytest.raises(exam.ExamError, match=r"the API refused a run's calls \(key_missing\)"):
        _paid(runner)
    assert [e for e, _ in runner.calls] == ["granskning", "kalkyl", "baseline"] and len(_burned()) == 1


# Reading what each engine's run met.


@pytest.mark.parametrize("engine", ["granskning", "kalkyl"])
def test_the_engines_failed_calls_are_read_from_their_summaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    """Granskning's `granskning.json` (its `degraded` section) and Kalkyl's `summary.json`, written by the child.
    Mutation: dropping `failures=` from `Engines.kalkyl` makes the Kalkyl case fail."""
    vroot = tmp_path / "variant"
    engines = exam.Engines(tmp_path / "logs", tmp_path, "main", None)
    run_id = "exam-prospective-t-0002-x"

    def child(vroot_: Path, args: list[str], log_name: str) -> subprocess.CompletedProcess[str]:
        if engine == "granskning":
            record = paths.company_dir("co-002", vroot_) / "runs" / run_id
            record.mkdir(parents=True)
            (record / "granskning.json").write_text(json.dumps({"degraded": {"failures": {"spend_cap": 2, "x": 0}}}))
        else:
            out = paths.dev_tender_dir("co-002", "t-0002", vroot_) / "kalkyl" / run_id
            out.mkdir(parents=True)
            (out / "summary.json").write_text(json.dumps({"failures": {"spend_cap": 2, "x": 0}}))
        return subprocess.CompletedProcess(args, 1, stdout="budget spent $0.00 of $40.00\ncost_usd: 0.0\n", stderr="")

    monkeypatch.setattr(engines, "_anbud", child)
    one = getattr(engines, engine)(vroot, "co-002", "t-0002", run_id)
    assert one.failures == {"spend_cap": 2}  # a kind with no failed call isn't one
    assert exam._breaks(one) == "the API refused a run's calls (spend_cap)"


def _calls_log(path: Path, calls: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [{"event": "start", "env_names": []}, *({"event": "call", **c} for c in calls), {"event": "end"}]
    path.write_text("".join(json.dumps(e) + "\n" for e in events) + "{torn")
    return path


def test_the_old_engines_failed_calls_are_read_from_the_preloads_log(tmp_path: Path) -> None:
    """Mutation: `elif False:` for the credit-balance branch of `_baseline_failures` makes it fail."""
    log = _calls_log(
        tmp_path / "a" / "calls.jsonl",
        [
            {"status": 200},
            {"status": 401, "error_kind": "authentication_error"},
            {"status": 403, "error_kind": "permission_error"},
            {"status": 400, "error_kind": "credit_balance"},
            {"status": 400, "error_kind": "invalid_request_error"},
            {"status": None, "error": "TypeError", "incomplete": True},
            {"status": 429},
            {"status": 529},
        ],
    )
    failures, unreachable = exam._baseline_failures(log)
    assert failures == {
        "auth": 2,
        "bad_request": 1,
        "connection": 1,
        "rate_limited": 1,
        "server": 1,
        "spend_cap": 1,
    }
    assert unreachable is False  # one call was answered
    none_answered = _calls_log(tmp_path / "b" / "calls.jsonl", [{"status": 529}, {"status": 529}])
    assert exam._baseline_failures(none_answered) == ({"server": 2}, True)
    assert exam._baseline_failures(tmp_path / "missing.jsonl") == (None, False)


@pytest.fixture
def old_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[exam.Engines, Path]:
    """The real `Engines.baseline` around a fake `run_baseline`: no export, no settings, no bun."""
    vroot = tmp_path / "variant"
    inbox = paths.dev_tender_dir("co-002", "t-0002", vroot)
    inbox.mkdir(parents=True)
    (inbox / "inbox.sha256").write_text("0" * 64 + "  a.pdf\n")
    engines = exam.Engines(tmp_path / "logs", tmp_path, "main", None)
    engines._app_sha = "0" * 40
    monkeypatch.setattr(engines, "_app_export", lambda: tmp_path / "app")
    monkeypatch.setattr(baseline, "matched_basis", lambda root=None: None)
    monkeypatch.setattr(provenance, "git_state", lambda root=None: provenance.GitState("a" * 40, False, None))
    return engines, vroot


def test_the_old_engine_reports_a_key_that_stopped_reading_and_a_run_that_reached_no_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old_engine: tuple[exam.Engines, Path]
) -> None:
    """A key that stops reading after the gate is the run's failure, not the exam's crash: nothing was sent, and the
    breaker stops before the next run (audit 2, repro a2_6). A run whose every call was refused reached no model.
    Mutations: dropping the `except KeyMissing` branch of `Engines.baseline`, or `unreachable=False`, each make it
    fail."""
    engines, vroot = old_engine
    run_dir = tmp_path / "exam" / "baseline" / "t-0002" / "r1"

    def no_key(**kwargs: Any) -> Any:
        kwargs["api_key"]()  # conftest: the key never reads under tests
        raise AssertionError("unreachable")

    monkeypatch.setattr(baseline, "run_baseline", no_key)
    one = engines.baseline(vroot, "co-002", "t-0002", "granskning", "r1", run_dir, dry_run=False)
    assert one.failures == {"key_missing": 1} and one.outputs_dir is None and one.cost_usd == 0.0
    assert exam._breaks(one) == "the API refused a run's calls (key_missing)"

    def refused(**kwargs: Any) -> Any:
        record = kwargs["record_dir"]
        _calls_log(record / "calls.jsonl", [{"status": 401, "error_kind": "authentication_error"}])
        (record / "run.json").write_text(json.dumps({"settings": None}))
        return SimpleNamespace(outcome="failed", exit_code=1, cost_usd=0.0)

    monkeypatch.setattr(baseline, "run_baseline", refused)
    one = engines.baseline(vroot, "co-002", "t-0002", "granskning", "r2", run_dir, dry_run=False)
    assert one.failures == {"auth": 1} and one.unreachable
    assert exam._breaks(one) == "a run had none of its calls answered"


# The API check, and no test reaching the API.


class _Sdk:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.counted: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(count_tokens=self.count_tokens)

    def count_tokens(self, **params: Any) -> Any:
        self.counted.append(params)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(input_tokens=8)


def test_the_api_check_sends_one_fixed_request_and_returns_only_a_kind() -> None:
    """Its one request is the map role's model, the message "ok" and a timeout: nothing of any tender. Mutation:
    dropping `timeout=` from the token count makes it fail."""
    config = load_config()
    ok = _Sdk()
    keys: list[str] = []
    assert model_client.check_api(config, lambda: "sk-test-value", lambda c, key: keys.append(key) or ok) is None
    assert keys == ["sk-test-value"]
    assert ok.counted == [
        {
            "model": config.model_for("map"),
            "messages": [{"role": "user", "content": "ok"}],
            "timeout": model_client.CHECK_TIMEOUT_S,
        }
    ]
    for error, kind in [
        (_status_error(anthropic.AuthenticationError, 401, "invalid x-api-key sk-test-value"), "auth"),
        (anthropic.APITimeoutError(request=REQUEST), "timeout"),
        (anthropic.APIConnectionError(request=REQUEST), "connection"),
    ]:
        assert model_client.check_api(config, lambda: "sk-test-value", lambda c, k, e=error: _Sdk(e)) == kind

    def no_key() -> str:
        raise model_client.KeyMissing("no Keychain entry")

    never: list[str] = []
    assert model_client.check_api(config, no_key, lambda c, k: never.append(k) or ok) == "key_missing" and never == []


class _Flaky(_Sdk):
    """Fails `n` times with its error, then answers."""

    def __init__(self, error: Exception, n: int) -> None:
        super().__init__(error)
        self.n = n

    def count_tokens(self, **params: Any) -> Any:
        self.counted.append(params)
        if len(self.counted) <= self.n:
            raise self.error  # type: ignore[misc]
        return SimpleNamespace(input_tokens=8)


def test_the_api_check_tries_a_transient_failure_again_and_never_a_refusal() -> None:
    """A blip on the free check would stop an unattended exam, so a transient kind gets the client's retries
    (`RETRY_DELAYS`, zeroed under tests). Mutation: `if True:` for the retry condition makes it fail."""
    config = load_config()
    tries = len(model_client.RETRY_DELAYS) + 1
    blip = _Flaky(anthropic.APIConnectionError(request=REQUEST), tries - 1)
    assert model_client.check_api(config, lambda: "sk-test-value", lambda c, k: blip) is None
    assert len(blip.counted) == tries
    down = _Flaky(anthropic.APIConnectionError(request=REQUEST), 99)
    assert model_client.check_api(config, lambda: "sk-test-value", lambda c, k: down) == "connection"
    assert len(down.counted) == tries
    refused = _Flaky(_status_error(anthropic.AuthenticationError, 401, "invalid x-api-key"), 99)
    assert model_client.check_api(config, lambda: "sk-test-value", lambda c, k: refused) == "auth"
    assert len(refused.counted) == 1


def test_the_exams_api_check_uses_the_workspace_key(monkeypatch: pytest.MonkeyPatch) -> None:
    engines = exam.Engines(Path("logs"), Path("."), "main", None)
    assert engines.api_check() == "key_missing"  # conftest: no test reads the Keychain
    accounts: list[str] = []
    monkeypatch.setattr(model_client, "keychain_key", lambda account: accounts.append(account) or "sk-test-value")
    monkeypatch.setattr(model_client, "make_sdk_client", lambda config, key: _Sdk())
    assert engines.api_check() is None and accounts == [load_config().workspace]


def test_no_test_can_read_the_key_or_build_a_real_client() -> None:
    """conftest's `_never_the_real_key` fails closed for the whole client: a path that gets a key some other way
    still can't build a client that reaches the API. Mutation: dropping `make_sdk_client` from its fakes makes it
    fail."""
    with pytest.raises(model_client.KeyMissing):
        model_client.keychain_key(account="anbud-dev")
    for module in (model_client, schema_check):  # schema_check holds its own reference, imported at the top
        with pytest.raises(RuntimeError, match="tests never build a real SDK client"):
            module.make_sdk_client(load_config(), "sk-test-value")
