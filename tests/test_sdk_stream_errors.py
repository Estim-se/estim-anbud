"""Any other exception from the SDK's own call is a failed call, not a crash (audit B3). A stream the real SDK opens
(HTTP 200, text/event-stream) and then can't finish for a reason that is neither the SDK's error nor an httpx transport
error: a malformed `data:` line (JSONDecodeError), a corrupt gzip body (httpx2.DecodingError), a body that ends before
`message_start` (AssertionError in `get_final_message`). These were re-raised raw, so no retry ran and no failed step
record was written. On a lens the frozen run couldn't be rebuilt at $0, and on the map or the verifier the run
crashed unfrozen. They are kind `stream` now, tried again like any transient failure. A bug in the harness's own code
still raises. The real SDK parses the broken bodies (an httpx2 MockTransport, no network, $0).

Each test names the one-line mutation it catches; each was made, seen to fail the test, and reverted."""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from granskning_fakes import ScriptedSDK, first_text
from test_granskning_pipeline import clean_tree, script, tender  # noqa: F401 - fixtures
from test_transient_retry import CLEAN, _is_lens, _is_map, _record, _run

from anbud import model_client
from anbud.config import load_config
from anbud.evals import freeze
from anbud.granskning.page_cards import SYSTEM as CARDS_SYSTEM
from anbud.model_client import TRANSIENT, Budget, ModelClient

_START = {
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


def _bad_json() -> tuple[dict[str, str], bytes]:
    body = f"event: message_start\ndata: {json.dumps(_START)}\n\nevent: content_block_start\ndata: {{not json\n\n"
    return {"content-type": "text/event-stream"}, body.encode()


def _bad_gzip() -> tuple[dict[str, str], bytes]:
    good = gzip.compress(f"event: message_start\ndata: {json.dumps(_START)}\n\n".encode())
    return {"content-type": "text/event-stream", "content-encoding": "gzip"}, good[:20] + b"\x00garbage" * 8


def _empty() -> tuple[dict[str, str], bytes]:
    return {"content-type": "text/event-stream"}, b"event: ping\ndata: {}\n\n"


BODIES = {"bad-json": _bad_json, "bad-gzip": _bad_gzip, "no-message-start": _empty}


class _Body(httpx2.SyncByteStream):
    def __init__(self, data: bytes) -> None:
        self.data = data

    def __iter__(self) -> Iterator[bytes]:
        yield self.data


def _real_sdk(make: Any) -> anthropic.Anthropic:
    def handler(request: httpx2.Request) -> httpx2.Response:
        headers, data = make()
        return httpx2.Response(200, headers=headers, stream=_Body(data))

    return anthropic.Anthropic(
        api_key="sk-test",
        base_url="http://mock.invalid",
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )


class BrokenOnceSDK(ScriptedSDK):
    """Scripted replies, except that the first call `when` picks is streamed by the real SDK from a broken body."""

    def __init__(self, when: Any, make: Any) -> None:
        super().__init__(script)
        self.when = when
        self.broken = 0
        self.real = _real_sdk(make)

    def stream(self, **params: Any) -> Any:
        if not self.broken and self.when(params):
            self.broken += 1
            self.sent.append(params)
            return self.real.messages.stream(**params)
        return super().stream(**params)


def _is_verifier(p: dict[str, Any]) -> bool:
    return p["system"] != CARDS_SYSTEM and "You are the verifier" in first_text(p)


@pytest.mark.parametrize("body", list(BODIES), ids=list(BODIES))
def test_a_lens_whose_stream_breaks_is_tried_again_and_its_run_rebuilds_at_zero(
    tender: Path,  # noqa: F811 - the fixture
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    body: str,
) -> None:
    """Mutation: `"timeout", "stream"})` -> `"timeout"})` in TRANSIENT: the broken call isn't tried again, and the lens
    is lost although the next attempt answers."""
    sdk = BrokenOnceSDK(_is_lens, BODIES[body])
    assert _run(monkeypatch, sdk, "t-0001-b3") == 0
    assert sdk.broken == 1 and freeze.is_frozen("co-001", "t-0001")
    assert _record("t-0001-b3")["degraded"] == {**CLEAN, "retries": 1}  # tried again, the work kept
    capsys.readouterr()
    sent = len(sdk.sent)
    code = _run(monkeypatch, sdk, "t-0001-b3-r1", "--replay", "--base-run", "t-0001-b3")
    assert code == 0, capsys.readouterr().err.strip().splitlines()[-1:]
    assert len(sdk.sent) == sent  # the rebuild sent nothing
    assert _record("t-0001-b3-r1")["degraded"] == _record("t-0001-b3")["degraded"]


@pytest.mark.parametrize("when", [_is_map, _is_verifier], ids=["map", "verifier"])
@pytest.mark.parametrize("body", list(BODIES), ids=list(BODIES))
def test_a_broken_stream_on_the_map_or_the_verifier_never_crashes_the_run(
    tender: Path,  # noqa: F811 - the fixture
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    when: Any,
    body: str,
) -> None:
    """Mutation: `return ModelError("stream", None, None, type(exc).__name__)` -> `raise exc` in `_mapped`: the SDK's
    exception escapes `anbud run`, which ends unfrozen, an exam FAILED pair."""
    sdk = BrokenOnceSDK(when, BODIES[body])
    code = _run(monkeypatch, sdk, "t-0001-b3m")
    assert sdk.broken == 1
    assert code == 0 and "frozen: ledger line 1, " in capsys.readouterr().out
    assert _record("t-0001-b3m")["degraded"] == {**CLEAN, "retries": 1}


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("x"),
        RuntimeError("x"),
        AssertionError("x"),
        json.JSONDecodeError("x", "{", 0),
        httpx2.DecodingError("x"),
        httpx2.StreamError("x"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_any_other_error_from_the_sdk_call_is_a_stream_failure_tried_again(exc: Exception) -> None:
    """Mutation: the same as above, `return ModelError("stream", ...)` -> `raise exc` in `_mapped`."""
    error = ModelClient._mapped(exc)
    assert (error.kind, error.status, error.message) == ("stream", None, type(exc).__name__)
    assert error.kind in TRANSIENT


def test_a_bug_in_the_harness_itself_is_still_raised() -> None:
    """Only the SDK's own calls are mapped: the harness's errors before them (here an unknown role) still raise."""
    client = ModelClient(load_config(), ScriptedSDK(script), Budget(1.0), run_id="r", company_id="co-001")
    with pytest.raises(KeyError):
        client.call(role="no_such_role", step="s", system=[], messages=[], max_tokens=1, effort="low")
    assert model_client.RETRY_DELAYS  # retried with the usual delays (zeroed here by conftest)
