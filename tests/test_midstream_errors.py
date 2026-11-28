"""An error event inside an open stream is classified by its own error type (audit B2). A stream that opened with HTTP
200 and then sends `event: error` (the usual form of an overload once a stream is open) makes the SDK raise a plain
APIStatusError(200). By status alone that was kind `status`, failed for good and never tried again. The real SDK parses
the stream here (an httpx2 MockTransport, no network, $0); the waits are zeroed (conftest).

Each test names the one-line mutation it catches; each was made, seen to fail the test, and reverted."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from anbud import model_client
from anbud.config import load_config
from anbud.model_client import Budget, ModelClient, ModelError

SYSTEM = [{"type": "text", "text": "synthetic system"}]
REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _start() -> bytes:
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "content": [],
        "model": "m",
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 1},
    }
    return _sse("message_start", {"type": "message_start", "message": message})


def _error_stream(kind: str) -> bytes:
    return _start() + _sse("error", {"type": "error", "error": {"type": kind, "message": "synthetic"}})


def _good_stream() -> bytes:
    block = {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
    delta = {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}}
    end = {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}}
    return b"".join(
        [
            _start(),
            _sse("content_block_start", block),
            _sse("content_block_delta", delta),
            _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse("message_delta", {**end, "usage": {"output_tokens": 2}}),
            _sse("message_stop", {"type": "message_stop"}),
        ]
    )


class _ErrorOnceSDK:
    """Token counts are faked; streams go through the real SDK. The first stream opens with HTTP 200 and ends in an
    error event of `kind`; the next one answers."""

    def __init__(self, kind: str) -> None:
        self.messages = self
        self.streams = 0

        def handler(request: httpx2.Request) -> httpx2.Response:
            self.streams += 1
            body = _error_stream(kind) if self.streams == 1 else _good_stream()
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)

        self.real = anthropic.Anthropic(
            api_key="sk-test",
            base_url="http://mock.invalid",
            max_retries=1,
            http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        )

    def count_tokens(self, **params: Any) -> Any:
        return SimpleNamespace(input_tokens=1000)

    def stream(self, **params: Any) -> Any:
        return self.real.messages.stream(**params)


def _call(client: ModelClient) -> Any:
    return client.call(
        role="map",
        step="s",
        system=SYSTEM,
        messages=[{"role": "user", "content": "synthetic"}],
        max_tokens=64,
        effort="low",
    )


@pytest.mark.parametrize("kind", ["overloaded_error", "api_error", "rate_limit_error"])
def test_an_error_event_inside_an_open_stream_is_tried_again(kind: str) -> None:
    """Mutation: `if (kind := BODY_KINDS.get(_body_error_type(exc))) is not None:` -> `if False:` in `classify`: the
    event is kind `status`, and the call fails for good after one stream."""
    sdk = _ErrorOnceSDK(kind)
    client = ModelClient(load_config(), sdk, Budget(10.0), run_id="r-b2", company_id="co-test")
    result = _call(client)
    assert sdk.streams == 2 and client.retries == 1
    assert result.message.content[0].text == "ok"


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        ({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}, "server"),
        ({"type": "error", "error": {"type": "api_error", "message": "Internal"}}, "server"),
        ({"type": "error", "error": {"type": "rate_limit_error", "message": "Slow down"}}, "rate_limited"),
        ({"type": "error", "error": {"type": "invalid_request_error", "message": "x"}}, "status"),  # not transient
        ({"type": "error", "error": "overloaded_error"}, "status"),  # not the API's shape
        (None, "status"),
    ],
)
def test_a_status_200_error_takes_its_kind_from_the_body(body: Any, kind: str) -> None:
    error = anthropic.APIStatusError("synthetic", response=httpx2.Response(200, request=REQUEST), body=body)
    classified = model_client.classify(error)
    assert (classified.kind, classified.status) == (kind, 200)
    assert isinstance(classified, ModelError)
