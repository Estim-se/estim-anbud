"""A transient error (a 429, a 529 or 5xx, one mid-stream too, a dropped connection, a timeout) is tried again, after a
wait, a bounded number of times. Each attempt reserves against the hard stop, so a retry the stop refuses takes the
budget's degrade path: the lens is marked (budget), never halted. A call that still fails is lost, and the run says
so: `degraded` in granskning.json (the lenses and the map lost, the attempts made again, the failures by kind), a line
in the report and on the terminal. Its step record keeps the failure, so a $0 rebuild loses the same work, while a paid
resume tries the call again. Synthetic tender, fake SDKs, $0; the waits are zeroed (conftest).

Each test names the one-line mutation it catches; each was made, seen to fail the test, and reverted."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from granskning_fakes import ScriptedSDK, first_text
from openpyxl import load_workbook
from test_granskning_pipeline import clean_tree, script, tender  # noqa: F401 - fixtures
from test_store import FakeSDK, _ask, _client

from anbud import cli, model_client
from anbud.evals import freeze
from anbud.granskning.page_cards import SYSTEM as CARDS_SYSTEM
from anbud.granskning.report import LOST
from anbud.model_client import Budget, ModelError
from anbud.store import RunStore

REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _overloaded() -> Exception:
    return anthropic.OverloadedError("overloaded", response=httpx2.Response(529, request=REQUEST), body=None)


def _rate_limited() -> Exception:
    return anthropic.RateLimitError("slow down", response=httpx2.Response(429, request=REQUEST), body=None)


def _connection() -> Exception:
    return anthropic.APIConnectionError(request=REQUEST)


class _MidStream:
    """A stream that opened, then broke: the SDK never retries this one."""

    def __init__(self, exc: Exception) -> None:
        self.exc, self.request_id = exc, "req_broken"

    def __enter__(self) -> _MidStream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get_final_message(self) -> Any:
        raise self.exc


class FlakySDK(ScriptedSDK):
    """Scripted replies, except that the first `times` calls `when` matches fail: before the stream opens (an error
    response), or mid-stream. `on_fail` runs as each one fails."""

    def __init__(self, when: Any, error: Any, times: int = 1, mid_stream: bool = False, on_fail: Any = None) -> None:
        super().__init__(script)
        self.when, self.error, self.times, self.mid_stream, self.on_fail = when, error, times, mid_stream, on_fail
        self.failed = 0

    def stream(self, **params: Any) -> Any:
        if self.failed < self.times and self.when(params):
            self.failed += 1
            self.sent.append(params)
            if self.on_fail:
                self.on_fail()
            if self.mid_stream:
                return _MidStream(self.error())
            raise self.error()
        return super().stream(**params)


def _is_lens(p: dict[str, Any]) -> bool:
    return p["system"] != CARDS_SYSTEM and "review lens `contract_terms`" in first_text(p)


def _is_map(p: dict[str, Any]) -> bool:
    return p["system"] != CARDS_SYSTEM and "Map this tender" in first_text(p)


def _run(monkeypatch: pytest.MonkeyPatch, sdk: Any, run_id: str, *extra: str) -> int:
    monkeypatch.setattr(model_client, "keychain_key", lambda **_: "sk-test")
    monkeypatch.setattr(model_client, "make_sdk_client", lambda config, key: sdk)
    return cli.main(["run", "co-001", "t-0001", "--run-id", run_id, *extra])


def _record(run_id: str) -> dict[str, Any]:
    return json.loads((RunStore("co-001").run_dir(run_id) / "granskning.json").read_text())


def _line(out: str, prefix: str) -> str:
    return [x for x in out.splitlines() if x.startswith(prefix)][-1]


CLEAN = {"lenses_lost": [], "map_lost": False, "failed_calls": 0, "failures": {}}


@pytest.mark.parametrize("when", [_is_lens, _is_map], ids=["lens", "map"])
@pytest.mark.parametrize(
    ("error", "mid_stream"),
    [(_overloaded, False), (_overloaded, True), (_rate_limited, False), (_connection, False)],
    ids=["529", "529-mid-stream", "429", "connection"],
)
def test_a_transient_error_is_tried_again_and_the_work_is_kept(
    tender: Path,  # noqa: F811 - the fixture
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    when: Any,
    error: Any,
    mid_stream: bool,
) -> None:
    """Mutation: `if error.kind not in TRANSIENT or delay is None:` -> `if True:`: no retry; the lens ends as an error
    with no flags, or the lenses run without the map."""
    sdk = FlakySDK(when, error, mid_stream=mid_stream)
    assert _run(monkeypatch, sdk, "t-0001-flaky") == 0
    out = capsys.readouterr().out
    assert sdk.failed == 1
    assert _record("t-0001-flaky")["degraded"] == {**CLEAN, "retries": 1}
    assert _line(out, "  map: ").startswith("  map: submitted")
    contract = _line(out, "  contract_terms: ")
    assert contract.startswith("  contract_terms: submitted") and " 0 flags" not in contract
    assert "degraded: 1 attempts made again; calls failed for good: none; map kept; lenses lost: none" in out


def test_a_lens_or_the_map_that_fails_every_attempt_is_lost_and_named_and_the_run_still_finishes(
    tender: Path,  # noqa: F811 - the fixture
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mutation: `lenses_lost, map_lost = lost(result)` -> `lenses_lost, map_lost = [], False` in `run_pipeline`: the
    record, the report and the terminal say nothing was lost."""
    attempts = 1 + len(model_client.RETRY_DELAYS)
    sdk = FlakySDK(lambda p: _is_lens(p) or _is_map(p), _rate_limited, times=2 * attempts)
    assert _run(monkeypatch, sdk, "t-0001-lost") == 0
    out = capsys.readouterr().out
    assert sdk.failed == 2 * attempts  # each call, its first attempt and every retry
    degraded = _record("t-0001-lost")["degraded"]
    retries = 2 * (attempts - 1)
    assert degraded == {
        "lenses_lost": ["contract_terms"],
        "map_lost": True,
        "retries": retries,
        "failed_calls": 2,
        "failures": {"rate_limited": 2},
    }
    run_dir = tender / "runs" / "t-0001-lost"
    page = (run_dir / "report" / "granskning.html").read_text(encoding="utf-8")
    named = "kartan (linserna kördes utan den), contract_terms (fel)"
    assert f"<strong>{LOST}</strong>: {named}" in page
    about = {
        r[0].value: r[1].value
        for r in load_workbook(run_dir / "report" / "granskning.xlsx")["Om rapporten"].iter_rows()
    }
    assert about[LOST] == named
    assert f"degraded: {retries} attempts made again; calls failed for good: rate_limited 2; map lost; " in out
    assert "frozen: ledger line 1, " in out and freeze.is_frozen("co-001", "t-0001")


def test_a_degraded_run_rebuilt_at_zero_loses_the_same_work(
    tender: Path,  # noqa: F811 - the fixture
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mutation: drop `self._record_failure(key, error, retried)` in `call`: the rebuild has no record of the lost lens,
    misses the step cache on it, and exits replay-incomplete without being frozen."""
    sdk = FlakySDK(_is_lens, _connection, times=99)
    assert _run(monkeypatch, sdk, "t-0001-lost") == 0
    capsys.readouterr()
    sent, paid = len(sdk.sent), tender / "runs" / "t-0001-lost"
    names = ("flags.json", "kronor.json", "items.json")
    before = {name: (paid / name).read_text() for name in names}
    rebuild = ["--replay", "--base-run", "t-0001-lost"]
    assert _run(monkeypatch, sdk, "t-0001-lost-r1", *rebuild) == 0
    out = capsys.readouterr().out
    assert "total $0.00" in out and "frozen: ledger line 2, " in out and len(sdk.sent) == sent
    rebuilt = tender / "runs" / "t-0001-lost-r1"
    assert {name: (rebuilt / name).read_text() for name in names} == before
    assert _record("t-0001-lost-r1")["degraded"] == _record("t-0001-lost")["degraded"]
    assert _record("t-0001-lost")["degraded"]["lenses_lost"] == ["contract_terms"]


class _Down(FakeSDK):
    def stream(self, **params: Any) -> Any:
        self.sent.append(params)
        raise _connection()


def test_a_paid_resume_tries_a_failed_call_again_while_a_replay_fails_it_the_same_way(tmp_path: Path) -> None:
    """Mutation: `if self.sdk is None:  # a replay fails it the same way` -> `if True:` in `_cached`: the resumed paid
    run fails the call again without sending it, so a resume can never recover the lost work."""
    store = RunStore("co-999", root=tmp_path)
    down = _Down()
    with pytest.raises(ModelError, match="connection"):
        _ask(_client(store, down), "q1")
    assert len(down.sent) == 1 + len(model_client.RETRY_DELAYS)

    replay = _client(store, None)  # $0: the recorded failure, the same way, nothing sent
    with pytest.raises(ModelError, match="connection"):
        _ask(replay, "q1")
    assert replay.failures == {"connection": 1} and replay.retries == len(model_client.RETRY_DELAYS)
    assert replay.replay_misses == 0

    up = FakeSDK()
    resumed = _client(store, up)  # paid, same run: tried again, and this time it answers
    result = _ask(resumed, "q1")
    assert len(up.sent) == 1 and not result.cached and resumed.failures == {}
    again = _client(store, None)  # the answer replaced the failure in the step record
    assert _ask(again, "q1").cached and again.failures == {}


def test_a_retry_the_hard_stop_refuses_marks_the_lens_budget_and_never_halts(
    tender: Path,  # noqa: F811 - the fixture
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The first attempt broke mid-stream and is charged its whole reservation; the stop is pulled in to what is spent
    and reserved at that moment, so the retry's reservation doesn't fit. Mutation: `if role in self.degrade_roles:` ->
    `if role in self.degrade_roles and not retried:` in `_send`: the refused retry halts the run (exit 5)."""
    budgets: list[Budget] = []

    def make(*args: Any, **kwargs: Any) -> Budget:
        budgets.append(Budget(*args, **kwargs))
        return budgets[-1]

    def pull_in() -> None:
        budgets[0].hard_stop = budgets[0].spent + budgets[0].reserved + 0.0001

    monkeypatch.setattr(model_client, "Budget", make)
    sdk = FlakySDK(_is_lens, _overloaded, mid_stream=True, on_fail=pull_in)
    assert _run(monkeypatch, sdk, "t-0001-refused-retry", "--concurrency", "1") == 0
    out = capsys.readouterr().out
    assert sdk.failed == 1 and budgets[0].spent <= budgets[0].hard_stop
    record = _record("t-0001-refused-retry")
    contract = next(lens for lens in record["lenses"] if lens["lens_id"] == "contract_terms")
    assert contract["error"] == "BudgetExceeded"
    assert "contract_terms" in record["budget"]["lenses_not_run"] + record["budget"]["lenses_stopped"]
    assert record["degraded"]["retries"] == 1 and record["degraded"]["lenses_lost"] == []  # the stop's, not an error's
    assert "frozen: ledger line 1, " in out and freeze.is_frozen("co-001", "t-0001")
