"""A connection that drops after the stream opened surfaces as httpx's own error, not the SDK's. It is a ModelError of
kind "connection" (or "timeout"), with the call's whole reservation charged, so each call site degrades as designed: a
card loses its tile, the lenses run without the map, the code merge stands, the flag goes unverified. And `anbud run`
prints "budget spent" on every exit, a crash too: the exam charges what that line says, or the whole tier without it.

The drop goes through the real `anthropic.Anthropic` client over an httpx MockTransport whose SSE body breaks after
`message_start`: no network, no key. Every other call is answered by the scripted fake. $0.

Each test names the one-line mutation it catches; each was made, seen to fail the test, and reverted."""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from granskning_fakes import ScriptedSDK, first_text
from test_granskning_pipeline import clean_tree, script, tender  # noqa: F401 - fixtures
from test_model_client import FakeMessages, _usage

from anbud import cli, model_client
from anbud.config import load_config
from anbud.granskning import pipeline
from anbud.granskning.merge import MERGE_SYSTEM
from anbud.granskning.page_cards import SYSTEM as CARDS_SYSTEM
from anbud.model_client import Budget, ModelClient, ModelError

DROP = "peer closed connection without sending complete message body"


class _BrokenBody(httpx2.SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        start = {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": "m",
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 1},
            },
        }
        yield f"event: message_start\ndata: {json.dumps(start)}\n\n".encode()
        raise httpx2.RemoteProtocolError(DROP)


def _real_sdk() -> anthropic.Anthropic:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=_BrokenBody())

    return anthropic.Anthropic(
        api_key="sk-test",
        base_url="http://mock.invalid",
        max_retries=1,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )


class DropOnceSDK(ScriptedSDK):
    """Scripted replies, except that the first call `when` matches is streamed by the real SDK and drops mid-stream."""

    def __init__(self, when: Any) -> None:
        super().__init__(script)
        self.when = when
        self.dropped = 0
        self.real = _real_sdk()

    def stream(self, **params: Any) -> Any:
        if not self.dropped and self.when(params):
            self.dropped += 1
            self.sent.append(params)
            return self.real.messages.stream(**params)
        return super().stream(**params)


def _is_card(p: dict[str, Any]) -> bool:
    return p["system"] == CARDS_SYSTEM


def _is_map(p: dict[str, Any]) -> bool:
    return p["system"] != CARDS_SYSTEM and "Map this tender" in first_text(p)


def _is_merge(p: dict[str, Any]) -> bool:
    return p["system"] != CARDS_SYSTEM and p["system"][0]["text"] == MERGE_SYSTEM


def _is_verifier(p: dict[str, Any]) -> bool:
    return p["system"] != CARDS_SYSTEM and "You are the verifier" in first_text(p)


def _paid(monkeypatch: pytest.MonkeyPatch, sdk: Any) -> None:
    monkeypatch.setattr(model_client, "keychain_key", lambda **_: "sk-test")
    monkeypatch.setattr(model_client, "make_sdk_client", lambda config, key: sdk)


@pytest.mark.parametrize("when", [_is_card, _is_map, _is_merge, _is_verifier], ids=["card", "map", "merge", "verifier"])
def test_a_stream_that_drops_mid_response_degrades_the_run_and_never_crashes_it(
    tender: Any,  # noqa: F811 - the fixture
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    when: Any,
) -> None:
    """Mutation: `if isinstance(exc, httpx2.TransportError):` -> `if False:` in `_mapped`: the drop is re-raised raw,
    and `anbud run` dies on it from every one of these calls."""
    sdk = DropOnceSDK(when)
    _paid(monkeypatch, sdk)
    code = cli.main(["run", "co-001", "t-0001", "--run-id", f"t-0001-drop-{when.__name__.strip('_')}"])
    out = capsys.readouterr().out
    assert sdk.dropped == 1  # the drop hit the chosen call
    assert code == 0 and "budget spent $" in out and "frozen: ledger line 1, " in out


def test_anbud_run_prints_its_spend_on_a_crash_too(
    tender: Any,  # noqa: F811 - the fixture
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mutation: the `finally:` in `run_command` -> `except OSError:`: a crash leaves no "budget spent" line, and the
    exam would charge the whole tier for it."""
    _paid(monkeypatch, ScriptedSDK(script))

    def crash(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("a bug")

    monkeypatch.setattr(pipeline, "run_granskning", crash)
    with pytest.raises(RuntimeError):
        cli.main(["run", "co-001", "t-0001", "--run-id", "t-0001-crash"])
    out = capsys.readouterr().out
    assert "budget spent $" in out and out.count("budget spent") == 1


def _drop(exc: Exception) -> FakeMessages:
    class Drops(FakeMessages):
        def stream(self, **params: Any) -> Any:
            self.sent.append(params)
            message = SimpleNamespace(usage=_usage(), stop_reason="end_turn", content=[])
            return _Opened(message, exc)

    return Drops()


class _Opened:
    def __init__(self, message: Any, exc: Exception) -> None:
        self.message, self.exc, self.request_id = message, exc, None

    def __enter__(self) -> _Opened:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get_final_message(self) -> Any:
        raise self.exc


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (httpx2.RemoteProtocolError(DROP), "connection"),
        (httpx2.ReadError("reset"), "connection"),
        (httpx2.WriteError("broken pipe"), "connection"),
        (httpx2.ReadTimeout("slow"), "timeout"),
    ],
)
def test_a_transport_error_mid_stream_is_a_model_error_with_the_whole_reservation_charged(
    error: Exception, kind: str
) -> None:
    """The message stays off the error's text, like the SDK's. Mutation: `if isinstance(exc, httpx2.TimeoutException):`
    -> `if False:`: a read timeout is logged as a dropped connection."""
    fake = _drop(error)
    client = ModelClient(load_config(), SimpleNamespace(messages=fake), Budget(5.0), run_id="r-1", company_id="co-x")
    with pytest.raises(ModelError) as caught:
        client.call(
            role="map",
            step="s",
            system=[{"type": "text", "text": "Prefix."}],
            messages=[{"role": "user", "content": "Hej"}],
            max_tokens=64,
            effort="low",
        )
    assert caught.value.kind == kind and DROP not in str(caught.value)
    attempts = 1 + len(model_client.RETRY_DELAYS)  # each one tried again (item 5), each attempt may have been billed
    assert len(fake.sent) == attempts and client.retries == attempts - 1
    price = client.config.price_for(client.config.model_for("map"))
    assert client.budget.spent == pytest.approx(attempts * price.max_cost(1000, 64))
    assert client.budget.reserved == pytest.approx(0) and client.halted == 0


def test_any_other_error_from_the_sdk_call_is_a_stream_failure() -> None:
    """Only `_send`'s SDK calls reach `_mapped`; anything else they raise is a failed call (test_sdk_stream_errors)."""
    error = ModelClient._mapped(ValueError("from the SDK"))
    assert (error.kind, error.message) == ("stream", "ValueError")
