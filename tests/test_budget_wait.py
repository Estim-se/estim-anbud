"""A call that doesn't fit under the hard stop while others are in flight waits for them, and is refused only when it
doesn't fit with nothing in flight (audit B5). Each call reserves the most it can cost, so several reservations in
flight could refuse a call, and halt Kalkyl or lose a Granskning lens, while what the calls really cost fits. Waiters
go in arrival order, so no later call takes the room ahead of an earlier one, and a call that ends in any way, an
interrupt included, releases its reservation. Each wait is an event (ids and numbers only). Both engines' paid runs
wait. Fakes and synthetic data only, $0.

Each test names the one-line mutation it catches; each was made, seen to fail the test, and reverted."""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from granskning_fakes import ScriptedSDK
from kalkyl_synthetic import _Stream, fake_sdk, good_handler, message, text, write_catalog, write_prep
from test_granskning_pipeline import clean_tree, script, tender  # noqa: F401 - fixtures
from test_model_unreachable import _Engines, _lens, _record

from anbud import cli, paths, store
from anbud.config import load_config
from anbud.granskning.loop import LENS_MAX_TOKENS
from anbud.kalkyl.run import run_dev_tender
from anbud.kalkyl.settings import EXTRACT, load_kalkyl_config
from anbud.model_client import Budget, BudgetExceeded, ModelClient, Waited

REQ = httpx2.Request("POST", "https://example.invalid/v1/messages")


def _until(done: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not done():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting")
        time.sleep(0.002)


def _queued(budget: Budget, n: int) -> Callable[[], bool]:
    return lambda: len(budget._queue) == n


class _Ordered(Budget):
    """A waiting Budget that records, inside its lock, the order it grants reservations in."""

    def __init__(self, hard_stop_usd: float) -> None:
        super().__init__(hard_stop_usd, wait=True)
        self.granted: list[float] = []

    def _take(self, usd: float) -> float:
        self.granted.append(usd)
        return super()._take(usd)


def _ask(budget: Budget, usd: float, out: dict[float, Any]) -> threading.Thread:
    def run() -> None:
        try:
            out[usd] = budget.take(usd)
        except BudgetExceeded as exc:
            out[usd] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_a_call_that_doesnt_fit_beside_one_in_flight_waits_for_it_and_then_runs() -> None:
    """Mutation: `if not self.wait or (not self._queue and self.in_flight == 0):` -> `if True:` in `Budget.take`: the
    call is refused at once, as without the wait, although it fits once the other call settles."""
    budget = _Ordered(1.0)
    budget.take(0.6)
    out: dict[float, Any] = {}
    asker = _ask(budget, 0.5, out)
    _until(lambda: len(budget._queue) == 1)
    assert 0.5 not in out  # it waits
    budget.settle(0.6, 0.1)
    asker.join(5)
    assert out[0.5] == (0.5, Waited(0.6, 1))
    assert (budget.spent, budget.in_flight) == (0.1, 1) and not budget._queue


def test_a_call_that_cant_fit_with_nothing_in_flight_is_refused_at_once_or_after_its_wait() -> None:
    """Mutation: `if self._queue[0] is ticket and self.in_flight == 0:` -> `if False:` in `Budget.take`: a call that
    can never fit waits forever."""
    budget = Budget(1.0, wait=True)
    budget.charge(0.5)
    with pytest.raises(BudgetExceeded) as refused:  # nothing in flight: no wait
        budget.take(0.6)
    assert refused.value.waited is None
    budget.take(0.3)
    out: dict[float, Any] = {}
    asker = _ask(budget, 0.6, out)
    _until(lambda: len(budget._queue) == 1)
    budget.settle(0.3, 0.3)  # spent 0.8: with nothing left in flight, 0.6 can never fit
    asker.join(5)
    assert not asker.is_alive()
    assert isinstance(out[0.6], BudgetExceeded) and out[0.6].waited == Waited(0.3, 1)
    assert budget.in_flight == 0 and not budget._queue


def test_waiters_go_in_arrival_order_and_a_later_call_never_takes_the_room_ahead_of_them() -> None:
    """Six calls, two settles. Mutation: `while not (self._queue[0] is ticket and self._fits(usd)):` -> `while not
    self._fits(usd):` in `Budget.take`: the small calls behind a large one take the room first, and it can wait for
    as long as small calls keep arriving."""
    budget = _Ordered(1.0)
    budget.take(0.9)
    out: dict[float, Any] = {}
    threads = []
    for usd in (0.5, 0.6, 0.05, 0.051):  # these queue in this order
        threads.append(_ask(budget, usd, out))
        _until(_queued(budget, len(threads)))
    budget.settle(0.9, 0.1)  # 0.5 fits now; 0.6 doesn't beside it, and the small ones behind it wait their turn
    _until(lambda: budget.granted == [0.9, 0.5] and len(budget._queue) == 3)
    threads.append(_ask(budget, 0.052, out))  # a new call that fits, arriving while others wait
    _until(lambda: len(budget._queue) == 4)
    assert budget.granted == [0.9, 0.5]
    budget.settle(0.5, 0.1)
    for thread in threads:
        thread.join(5)
    assert budget.granted == [0.9, 0.5, 0.6, 0.05, 0.051, 0.052]
    assert budget.in_flight == 4 and not budget._queue  # six taken, two settled


class _Killed(BaseException):
    """The call dies mid-stream as a kill or Ctrl-C would: not an Exception."""


class _TwoCalls:
    """A fake SDK for calls whose reservations don't fit together: the first one's stream waits until `waiting`
    others are waiting for room, then ends with `first` (an exception to raise, or None to answer)."""

    def __init__(self, budget: Budget, tokens: int, first: BaseException | None, waiting: int = 1) -> None:
        self.messages = self
        self.budget, self.tokens, self.first, self.waiting = budget, tokens, first, waiting
        self.streams = 0
        self._lock = threading.Lock()

    def count_tokens(self, **params: Any) -> Any:
        return SimpleNamespace(input_tokens=self.tokens)

    def stream(self, **params: Any) -> Any:
        with self._lock:
            self.streams += 1
            n = self.streams
        if n == 1:
            _until(lambda: len(self.budget._queue) == self.waiting)
            if self.first is not None:
                raise self.first
        return _Stream(message([text("ok")], stop_reason="end_turn"))


def _two_calls(first: BaseException | None, store_: store.RunStore | None = None) -> tuple[ModelClient, dict, Budget]:
    """Call `a` in flight, `b` waiting for its room. Returns what each ended with."""
    config = load_config()
    price = config.price_for(config.model_for(EXTRACT))
    tokens = 100_000
    one = price.max_cost(tokens, 1000)
    budget = Budget(1.5 * one, wait=True)  # one call fits, two don't
    client = ModelClient(
        config, _TwoCalls(budget, tokens, first), budget, run_id="r-b5", company_id="co-001", store=store_
    )
    out: dict[str, Any] = {}

    def call(step: str) -> None:
        try:
            out[step] = client.call(
                role=EXTRACT,
                step=step,
                system=[{"type": "text", "text": "Synthetic."}],
                messages=[{"role": "user", "content": step}],
                max_tokens=1000,
                effort="low",
            )
        except BaseException as exc:  # noqa: BLE001 - the test reads how each call ended
            out[step] = exc

    a = threading.Thread(target=call, args=("a",), daemon=True)
    a.start()
    _until(lambda: budget.in_flight == 1)
    b = threading.Thread(target=call, args=("b",), daemon=True)
    b.start()
    a.join(5)
    b.join(5)
    assert not a.is_alive() and not b.is_alive(), "a call is still waiting for room"
    return client, out, budget


def test_a_call_in_flight_that_fails_releases_its_room_and_the_waiting_call_runs() -> None:
    """A 500 before the stream opened is unbilled: its room goes back, the waiting call runs, the failed one is tried
    again after it. Mutation: `self.budget.settle(reserved, charged)` -> deleted in `_send`'s error branch: the failed
    call's room is never released, and the waiting call (and the retry behind it) waits forever."""
    error = anthropic.InternalServerError("boom", response=httpx2.Response(500, request=REQ), body=None)
    client, out, budget = _two_calls(error)
    assert not isinstance(out["b"], BaseException)  # the waiting call ran
    assert not isinstance(out["a"], BaseException) and client.retries == 1  # the failed one ran again, after it
    assert (budget.in_flight, budget.reserved) == (0, pytest.approx(0))


def test_a_call_in_flight_that_dies_still_releases_its_room_so_no_call_waits_forever() -> None:
    """A BaseException (a kill, Ctrl-C) mid-stream: its reservation stays spent, as it may have been billed, and the
    waiting call is no longer held. Mutation: `if not settled:` -> `if False:` in `_send`: the dead call's room is
    never released, and the waiting call waits forever."""
    _, out, budget = _two_calls(_Killed())
    assert isinstance(out["a"], _Killed)
    assert isinstance(out["b"], BudgetExceeded)  # the dead call's room is spent, so it can't fit; it isn't left waiting
    assert (budget.in_flight, budget.reserved) == (0, pytest.approx(0))


def test_a_call_that_waited_records_a_wait_event_with_numbers_only(data_root: Path) -> None:
    """Mutation: `if waited is not None:` -> `if False:` in `ModelClient._waited`: a wait leaves no trace, and the
    eval lane can't see that one happened."""
    _, out, _ = _two_calls(None, store.RunStore("co-001"))
    assert not isinstance(out["a"], BaseException) and not isinstance(out["b"], BaseException)
    lines = (paths.company_dir("co-001") / "runs" / "r-b5" / "events.jsonl").read_text().splitlines()
    waits = [json.loads(line) for line in lines if json.loads(line)["kind"] == "budget_wait"]
    assert [w["step"] for w in waits] == ["b"]
    wait = waits[0]
    assert wait["in_flight"] == 1 and wait["reserved_usd"] > 0 and wait["reserve_usd"] == wait["reserved_usd"]
    assert wait["duration_ms"] >= 0
    shown = {k for k, v in wait.items() if v is not None}
    assert shown == {
        "run_id",
        "company_id",
        "step",
        "kind",
        "at",
        "duration_ms",
        "reserve_usd",
        "reserved_usd",
        "in_flight",
    }


class _HookFailed(Exception):
    """An event hook's own failure, not the call's."""


def test_an_event_hook_that_fails_on_a_wait_still_releases_the_room_so_no_call_behind_it_hangs() -> None:
    """Call `a` in flight, `b` and `c` waiting behind it. When `b` gets the room, the hook raises on its wait event:
    `b` fails before it is sent, its room is released (kept as spent, erring high), and `c` is no longer held.
    Mutation: `self._waited(step, asked, reserve, waited)` moved back before the guarded `try` in `_send`: `b`'s room
    is never released, and `c` waits forever."""
    config = load_config()
    price = config.price_for(config.model_for(EXTRACT))
    tokens = 100_000
    budget = Budget(1.5 * price.max_cost(tokens, 1000), wait=True)  # one call fits, two don't

    def hook(event: Any) -> None:
        if event.kind == "budget_wait" and event.step == "b":
            raise _HookFailed

    sdk = _TwoCalls(budget, tokens, None, waiting=2)
    client = ModelClient(config, sdk, budget, run_id="r-b5", company_id="co-001", on_event=hook)
    out: dict[str, Any] = {}

    def call(step: str) -> None:
        try:
            out[step] = client.call(
                role=EXTRACT,
                step=step,
                system=[{"type": "text", "text": "Synthetic."}],
                messages=[{"role": "user", "content": step}],
                max_tokens=1000,
                effort="low",
            )
        except BaseException as exc:  # noqa: BLE001 - the test reads how each call ended
            out[step] = exc

    threads = []
    # a's stream holds until b and c both wait (`_TwoCalls`), so c needs no wait here: it may be done by then
    for step, ready in (("a", lambda: budget.in_flight == 1), ("b", _queued(budget, 1)), ("c", lambda: True)):
        threads.append(threading.Thread(target=call, args=(step,), daemon=True))
        threads[-1].start()
        _until(ready)
    for thread in threads:
        thread.join(5)
    assert not any(t.is_alive() for t in threads), "a call is still waiting for room"
    assert not isinstance(out["a"], BaseException) and isinstance(out["b"], _HookFailed)
    assert isinstance(out["c"], BudgetExceeded)  # b's room stays spent, so c can't fit; it isn't left waiting
    assert sdk.streams == 1  # b was never sent
    assert (budget.in_flight, budget.reserved) == (0, pytest.approx(0))


def _reading(params: dict[str, Any]) -> bool:
    return bool(params.get("tools")) and params["tools"][0]["name"] == "submit_quantities"


def _tokens_for(price: Any, usd: float, max_tokens: int) -> int:
    """Input tokens whose call, with `max_tokens`, reserves `usd` (`Price.max_cost`)."""
    dearest = max(price.input, price.cache_write_5m, price.cache_write_1h)
    return int((usd / 1.05 * 1e6 - max_tokens * price.output) / dearest)


class _Heavy:
    """A fake SDK whose calls `heavy` picks count `tokens` input tokens and take a moment, so they overlap; their real
    cost stays small. Everything else answers as `inner` does."""

    def __init__(self, inner: Any, heavy: Callable[[dict[str, Any]], bool], tokens: int) -> None:
        self.inner, self.heavy, self.tokens = inner, heavy, tokens
        self.messages = self

    def count_tokens(self, **params: Any) -> Any:
        return SimpleNamespace(input_tokens=self.tokens if self.heavy(params) else 1000)

    def stream(self, **params: Any) -> Any:
        if self.heavy(params):
            time.sleep(0.1)  # in flight long enough for the next call to ask for its room
        return self.inner.stream(**params)


def test_kalkyl_s_reading_calls_wait_for_room_instead_of_halting_and_the_run_rebuilds_byte_for_byte(
    data_root: Path,
) -> None:
    """Reading calls that each reserve 60% of the S stop and cost cents: two can't be in flight together. Mutation:
    `Budget(stop.usd, wait=True)` -> `Budget(stop.usd)` in `run_dev_tender`: the second call in flight is refused and
    the calc halts incomplete, although the run costs cents."""
    tender_dir = paths.dev_tender_dir("co-001", "t-0001")
    write_prep(tender_dir / "prep")
    write_catalog(data_root)
    kalkyl = dataclasses.replace(load_kalkyl_config(), chunk_max_quantity_lines=1)  # one chunk per marked line
    config = load_config()
    price = config.price_for(config.model_for(EXTRACT))
    stop = kalkyl.stops[0].usd
    tokens = _tokens_for(price, 0.6 * stop, kalkyl.roles[EXTRACT].max_tokens)
    sdk = _Heavy(fake_sdk(good_handler).messages, _reading, tokens)
    paid = run_dev_tender("co-001", "t-0001", sdk=sdk, catalog_name="synthetic", run_id="kalkyl-b5", kalkyl=kalkyl)
    assert paid["chunks"] >= 3 and paid["stop"]["tier"] == "S"
    assert paid["complete"] and paid["halted"] == 0 and paid["cost_usd"] < 0.1 * stop
    events = (paths.company_dir("co-001") / "runs" / "kalkyl-b5" / "events.jsonl").read_text().splitlines()
    assert any(json.loads(line)["kind"] == "budget_wait" for line in events)
    rebuilt = run_dev_tender(
        "co-001",
        "t-0001",
        sdk=None,
        catalog_name="synthetic",
        run_id="kalkyl-b5-r",
        base_run="kalkyl-b5",
        kalkyl=kalkyl,
    )
    assert rebuilt["complete"] and rebuilt["cost_usd"] == 0 and rebuilt["replay_misses"] == 0
    base, again = tender_dir / "kalkyl" / "kalkyl-b5", tender_dir / "kalkyl" / "kalkyl-b5-r"
    assert (again / "posts.json").read_bytes() == (base / "posts.json").read_bytes()
    # Everything else the rebuild wrote is the paid run's, but what each chunk cost: a rebuild costs nothing.
    assert _costless(json.loads((again / "result.json").read_bytes())) == _costless(
        json.loads((base / "result.json").read_bytes())
    )


def _costless(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _costless(v) for k, v in value.items() if k != "cost_usd"}
    if isinstance(value, list):
        return [_costless(v) for v in value]
    return value


def _granskning(data_root: Path, monkeypatch: pytest.MonkeyPatch, sdk: Any, run_id: str) -> Any:
    """`anbud run` on the synthetic tender with `sdk`, read by the exam's Engines (`test_model_unreachable`)."""
    from anbud import model_client

    monkeypatch.setattr(model_client, "keychain_key", lambda **_: "sk-test")
    monkeypatch.setattr(model_client, "make_sdk_client", lambda config, key: sdk)
    engines = _Engines(data_root / "logs", Path("/nonexistent-app"), "HEAD", None)
    return engines.granskning(data_root, "co-001", "t-0001", run_id)


def _lens_tokens(share: float) -> int:
    """Input tokens that make a lens call reserve `share` of Granskning's S stop."""
    config = load_config()
    price = config.price_for(config.model_for("lens"))
    stop = config.budgets["granskning_stops"][0]["hard_stop"]
    return _tokens_for(price, share * stop, LENS_MAX_TOKENS)


def test_granskning_s_lenses_wait_for_room_instead_of_losing_a_lens(
    tender: Path,  # noqa: F811 - the fixture
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lens calls that each reserve 40% of the S stop and cost cents, three lenses at a time. Mutation:
    `Budget(ceiling.usd, wait=True)` -> `Budget(ceiling.usd)` in `run_command`: lenses whose calls overlap are refused
    and lost, a degraded run whose spend fits many times over."""
    sdk = _Heavy(ScriptedSDK(script), lambda p: _lens(p) is not None, _lens_tokens(0.4))
    one = _granskning(data_root, monkeypatch, sdk, "t-0001-b5")
    assert (one.exit_code, one.status) == (0, "finished")
    record = _record(data_root, "t-0001-b5")
    assert record["degraded"]["lenses_lost"] == [] and record["degraded"]["failed_calls"] == 0
    budget = record["budget"]  # what the hard stop cost the run: nothing
    assert (budget["refused_calls"], budget["lenses_not_run"], budget["lenses_stopped"]) == (0, [], [])
    events = (paths.company_dir("co-001") / "runs" / "t-0001-b5" / "events.jsonl").read_text().splitlines()
    assert any(json.loads(line)["kind"] == "budget_wait" for line in events)
    # The $0 rebuild sends nothing and writes what the paid run wrote.
    sent = len(sdk.inner.sent)
    assert cli.main(["run", "co-001", "t-0001", "--run-id", "t-0001-b5-r", "--replay", "--base-run", "t-0001-b5"]) == 0
    assert len(sdk.inner.sent) == sent
    base, again = (paths.dev_tender_dir("co-001", "t-0001") / "runs" / run for run in ("t-0001-b5", "t-0001-b5-r"))
    for name in ("flags.json", "items.json", "kronor.json"):
        assert (again / name).read_bytes() == (base / name).read_bytes(), name


def test_granskning_still_loses_a_lens_whose_call_cant_fit_alone(
    tender: Path,  # noqa: F811 - the fixture
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boundary: a lens call bigger than the whole stop is refused (after any wait) and the lens is lost, the
    true out-of-budget degrade; the other lenses run."""
    huge = _lens_tokens(1.2)
    sdk = _Heavy(ScriptedSDK(script), lambda p: _lens(p) == "regulation", huge)
    one = _granskning(data_root, monkeypatch, sdk, "t-0001-b5x")
    assert (one.exit_code, one.status) == (0, "finished")
    budget = _record(data_root, "t-0001-b5x")["budget"]
    assert (budget["refused_calls"], budget["lenses_not_run"]) == (1, ["regulation"])
