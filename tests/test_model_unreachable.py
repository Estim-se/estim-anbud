"""A run that reviewed nothing never finishes (audit B1). When calls failed for good, of any kind, and no lens call was
answered (by the model or the step cache), whatever happened to the map, `anbud run` exits MODEL_UNREACHABLE (6) and
freezes nothing. `anbud kalkyl` does the same when no reading call was answered. A run with one lens answered stays a
degraded, finished run. The exam's own Engines parse how each run ended. Synthetic data and fakes only, $0.

Each test names the one-line mutation it catches; each was made, seen to fail the test, and reverted."""

from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from granskning_fakes import first_text
from kalkyl_synthetic import add_tile, good_handler, write_catalog, write_prep
from test_granskning_pipeline import clean_tree, script, tender  # noqa: F401

from anbud import cli, model_client, paths, store
from anbud.config import load_config
from anbud.evals import exam, freeze
from anbud.evals import split_heldout as sh
from anbud.granskning import provenance
from anbud.granskning.command import MODEL_UNREACHABLE, RUN_HALTED
from anbud.model_client import Budget, ModelClient

REQ = httpx2.Request("POST", "https://example.invalid/v1/messages")
Fail = Callable[[], Exception]


def _auth() -> Exception:
    return anthropic.AuthenticationError("invalid x-api-key", response=httpx2.Response(401, request=REQ), body=None)


def _credit() -> Exception:
    message = "Your credit balance is too low to access the Anthropic API."
    return anthropic.BadRequestError(message, response=httpx2.Response(400, request=REQ), body=None)


def _offline() -> Exception:
    return anthropic.APIConnectionError(request=REQ)


def _server() -> Exception:
    return anthropic.InternalServerError("Internal server error", response=httpx2.Response(500, request=REQ), body=None)


def _lens(params: dict[str, Any]) -> str | None:
    m = re.search(r"review lens `([a-z_]+)`", first_text(params)) if "tools" in params else None
    return m.group(1) if m else None


def _failing(fail: Fail, when: Callable[[dict[str, Any]], bool]) -> Callable[[dict[str, Any]], Any]:
    """The pipeline's script, except that the requests `when` picks fail with `fail()`."""

    def handler(params: dict[str, Any]) -> Any:
        if when(params):
            raise fail()
        return script(params)

    return handler


class _Engines(exam.Engines):
    """The exam's Engines, with the child `anbud` run in this process so the fake SDK is used."""

    def _anbud(self, vroot: Path, args: list[str], log_name: str) -> subprocess.CompletedProcess[str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(args)
        return subprocess.CompletedProcess(args, code, out.getvalue(), err.getvalue())


def _granskning(data_root: Path, monkeypatch: pytest.MonkeyPatch, handler: Any, run_id: str) -> exam.EngineRun:
    from granskning_fakes import ScriptedSDK

    sdk = ScriptedSDK(handler)
    monkeypatch.setattr(model_client, "keychain_key", lambda **_: "sk-test")
    monkeypatch.setattr(model_client, "make_sdk_client", lambda config, key: sdk)
    engines = _Engines(data_root / "logs", Path("/nonexistent-app"), "HEAD", None)
    return engines.granskning(data_root, "co-001", "t-0001", run_id)


def _record(data_root: Path, run_id: str) -> dict[str, Any]:
    path = paths.company_dir("co-001", data_root) / "runs" / run_id / "granskning.json"
    return json.loads(path.read_text())


@pytest.mark.parametrize(
    ("fail", "kind"),
    [(_auth, "auth"), (_credit, "spend_cap"), (_offline, "connection"), (_server, "server")],
    ids=["revoked-key-401", "credit-balance-400", "offline-after-retries", "server-after-retries"],
)
def test_a_run_whose_every_call_failed_exits_6_and_is_never_frozen(
    tender: Path,  # noqa: F811
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail: Fail,
    kind: str,
) -> None:
    """Mutation: `if client.unanswered(LENS_STEP):` -> `if False:` in `run_command`: the run exits 0 and freezes as a
    finished review with no flags (the credit case exits 5, halted)."""
    run_id = f"t-0001-dead-{kind.replace('_', '')}"
    one = _granskning(data_root, monkeypatch, _failing(fail, lambda _: True), run_id)
    assert (one.exit_code, one.status) == (MODEL_UNREACHABLE, freeze.FAILED)
    assert freeze.frozen_line("co-001", run_id) is None
    summary = exam.degraded_summary(one.budget, one.lost) or {}
    assert set(summary["failures"]) == {kind}
    # The spend cap halts the run at the map, so its lenses never start; otherwise each is tried and lost.
    assert summary["lenses_lost"] == (0 if kind == "spend_cap" else 8)


def test_a_credit_balance_400_is_the_spend_cap() -> None:
    """Mutation: `if "usage limit" in lowered or "credit balance" in lowered:` -> `if "usage limit" in lowered:` in
    `classify`: an empty balance is a bad request, and the run goes on calling into it instead of pausing."""
    error = model_client.classify(_credit())
    assert (error.kind, error.status) == ("spend_cap", 400)
    assert (
        model_client.classify(
            anthropic.BadRequestError("bad", response=httpx2.Response(400, request=REQ), body=None)
        ).kind
        == "bad_request"
    )


def test_a_run_with_a_map_but_every_lens_lost_exits_6(
    tender: Path,  # noqa: F811
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: `LENS_STEP = "lens:"` -> `LENS_STEP = ""`: the answered map (and cards) count as a review, and the
    run freezes as finished with no lens."""
    run_id = "t-0001-map-only"
    one = _granskning(data_root, monkeypatch, _failing(_auth, lambda p: _lens(p) is not None), run_id)
    assert _record(data_root, run_id)["degraded"]["map_lost"] is False  # the map was answered
    assert (one.exit_code, one.status) == (MODEL_UNREACHABLE, freeze.FAILED)
    assert freeze.frozen_line("co-001", run_id) is None


def test_a_run_with_one_lens_answered_stays_degraded_and_finished(
    tender: Path,  # noqa: F811
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: `return bool(self.failures) and not any(s.startswith(prefix) for s in self.answered)` ->
    `return bool(self.failures)` in `ModelClient.unanswered`: any failed call stops a run that has a review.

    The one lens answered is generic_scope, on its own role, which the checks after the lenses share: counting by
    step, not role, is what tells them apart."""
    run_id = "t-0001-one-lens"
    one = _granskning(
        data_root, monkeypatch, _failing(_auth, lambda p: _lens(p) not in (None, "generic_scope")), run_id
    )
    assert (one.exit_code, one.status) == (0, freeze.FINISHED)
    summary = exam.degraded_summary(one.budget, one.lost) or {}
    assert summary["lenses_lost"] == 7 and set(summary["failures"]) == {"auth"}


def test_a_run_stopped_before_its_lenses_with_nothing_failed_stays_halted(
    tender: Path,  # noqa: F811
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: `return bool(self.failures) and not any(...)` -> `return not any(...)` in `ModelClient.unanswered`: a
    stop marker before the lenses reads as a dead model, and would trip the eval lane's circuit breaker."""
    run_id = "t-0001-stopped"

    def stop_at_map(params: dict[str, Any]) -> Any:
        if "tools" in params and "Map this tender" in first_text(params):
            store.request_stop("co-001", run_id)
        return script(params)

    one = _granskning(data_root, monkeypatch, stop_at_map, run_id)
    assert (one.exit_code, one.status) == (RUN_HALTED, freeze.HALTED)


def test_unanswered_counts_steps_not_roles(data_root: Path) -> None:
    client = ModelClient(load_config(), None, Budget(1.0), run_id="r", company_id="co-001")
    assert not client.unanswered("lens:")  # nothing failed
    client._count("auth", 0)
    assert client.unanswered("lens:")
    client._answer("check_space_rules/t1")  # a check on a lens role
    assert client.unanswered("lens:")
    client._answer("lens:generic_scope/t1")
    assert not client.unanswered("lens:")


def test_kalkyl_exits_6_when_no_reading_call_was_answered(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: `if summary.get("unanswered"):` -> `if False:` in `_kalkyl`: Kalkyl exits 0 for a dead key, and the
    eval lane's circuit breaker can't tell it from an incomplete calc. Its page card is answered, so the rule counts
    the reading's steps only."""
    tender_dir = paths.dev_tender_dir("co-001", "t-0001")
    write_prep(tender_dir / "prep")
    add_tile(tender_dir / "prep", store.RunStore("co-001"))
    write_catalog(data_root)
    sh.append_events(paths.ledger_path("co-001"), [sh._assign("t-0001", sh.DEV, "test", ())])
    from kalkyl_synthetic import fake_sdk

    def handler(params: dict[str, Any]) -> Any:
        if params.get("tools") and params["tools"][0]["name"] == "submit_quantities":
            raise _auth()
        return good_handler(params)  # the page card (and R3, if it runs) answer

    sdk = fake_sdk(handler)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(model_client, "keychain_key", lambda **_: "sk-test")
    monkeypatch.setattr(model_client, "make_sdk_client", lambda config, key: sdk)
    monkeypatch.setattr(provenance, "git_state", lambda root=None: provenance.GitState("a" * 40, False, None))
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["kalkyl", "co-001", "t-0001", "--catalog", "synthetic", "--run-id", "kalkyl-dead"])
    assert code == MODEL_UNREACHABLE
    assert any("tools" not in p for p in sdk.messages.sent)  # the card was read
    assert "no reading call was answered (calls failed for good: auth " in err.getvalue()
    assert "frozen: False" in out.getvalue() and "sk-test" not in out.getvalue() + err.getvalue()
