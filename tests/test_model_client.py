"""ModelClient v0 against a fake SDK client. No network, no key, $0."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import CacheCreation, Usage

from anbud import model_client, schema_check
from anbud.config import Price, load_config
from anbud.model_client import Budget, BudgetExceeded, KeyMissing, ModelClient, ModelError, PrefixDrift, classify

SYSTEM = [{"type": "text", "text": "Synthetic system block."}]
MESSAGES = [{"role": "user", "content": "Synthetic question."}]
REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _usage(**overrides: Any) -> Usage:
    fields = {
        "input_tokens": 1000,
        "output_tokens": 100,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    return Usage(**{**fields, **overrides})


def _status_error(cls: type, status: int, message: str) -> Exception:
    return cls(message, response=httpx2.Response(status, request=REQUEST), body=None)


class FakeStream:
    """Like the SDK's: the request id is on the stream, not on the final message."""

    def __init__(self, message: Any, mid_stream_error: Exception | None = None) -> None:
        self.message = message
        self.mid_stream_error = mid_stream_error
        self.request_id = "req_stream"

    def __enter__(self) -> FakeStream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get_final_message(self) -> Any:
        if self.mid_stream_error:
            raise self.mid_stream_error
        return self.message


class FakeMessages:
    def __init__(
        self, usage: Usage | None = None, error: Exception | None = None, mid_stream_error: Exception | None = None
    ) -> None:
        self.usage = usage or _usage()
        self.error = error
        self.mid_stream_error = mid_stream_error
        self.sent: list[dict[str, Any]] = []
        self.counted: list[dict[str, Any]] = []

    def count_tokens(self, **params: Any) -> Any:
        self.counted.append(params)
        return SimpleNamespace(input_tokens=1000)

    def stream(self, **params: Any) -> FakeStream:
        self.sent.append(params)
        if self.error:
            raise self.error  # an error response: the stream never opens
        message = SimpleNamespace(usage=self.usage, stop_reason="end_turn", content=[])
        return FakeStream(message, self.mid_stream_error)

    def create(self, **params: Any) -> Any:
        raise AssertionError("ModelClient must stream")


def _client(messages: FakeMessages | None = None, hard_stop: float = 5.0, events: list | None = None) -> ModelClient:
    sdk = SimpleNamespace(messages=messages or FakeMessages())
    on_event = events.append if events is not None else None
    return ModelClient(load_config(), sdk, Budget(hard_stop), run_id="r-1", company_id="co-test", on_event=on_event)


def _call(client: ModelClient, role: str = "map", system: list | None = None, **kw: Any) -> Any:
    args = {"step": "s", "system": system or SYSTEM, "messages": MESSAGES, "max_tokens": 64, "effort": "low", **kw}
    return client.call(role=role, **args)


def test_a_call_streams_the_plain_request_with_the_shared_tools() -> None:
    fake = FakeMessages()
    client = _client(fake)
    _call(client, "map")
    _call(client, "lens")
    _call(client, "verifier")
    _call(client, "page_cards", system=[{"type": "text", "text": "Page cards."}])
    map_params, lens_params, verifier_params, cards_params = fake.sent
    assert map_params["model"] == "claude-opus-5-5" and cards_params["model"] == "claude-sonnet-5-5"
    assert map_params["max_tokens"] == 64 and map_params["output_config"] == {"effort": "low"}
    assert map_params["tool_choice"] == {"type": "auto"}
    assert map_params["tools"] == lens_params["tools"] == verifier_params["tools"]
    assert len(map_params["tools"]) == 10
    assert "tools" not in cards_params and "tool_choice" not in cards_params
    for params in fake.sent:
        assert not {"thinking", "temperature", "top_p", "top_k"} & params.keys()
    assert "max_tokens" not in fake.counted[0]


def test_cost_comes_from_usage_and_the_budget_is_settled() -> None:
    writes = CacheCreation(ephemeral_5m_input_tokens=0, ephemeral_1h_input_tokens=2000)
    usage = _usage(cache_creation_input_tokens=2000, cache_creation=writes, cache_read_input_tokens=5000)
    events: list = []
    client = _client(FakeMessages(usage), events=events)
    result = _call(client)
    expected = (1000 * 4.0 + 100 * 20.0 + 2000 * 8.0 + 5000 * 0.2) / 1_000_000
    assert result.cost_usd == pytest.approx(expected)
    assert result.request_id == "req_stream"
    assert client.budget.spent == pytest.approx(expected) and client.budget.reserved == pytest.approx(0)
    (event,) = events
    assert event.kind == "model_call" and event.cost_usd == pytest.approx(expected)
    assert event.input_tokens == 1000 and event.cache_read_tokens == 5000


def test_a_call_past_the_hard_stop_is_refused_unsent() -> None:
    fake = FakeMessages()
    client = _client(fake, hard_stop=0.001)
    with pytest.raises(BudgetExceeded):
        _call(client)
    assert fake.sent == []


@pytest.mark.parametrize(
    ("messages", "hard_stop", "halted"),
    [
        (FakeMessages(), 0.001, 1),  # the run's own hard stop
        (FakeMessages(error=_status_error(anthropic.EnforcedSpendLimitReachedError, 402, "limit")), 10.0, 1),
        (FakeMessages(error=_status_error(anthropic.RateLimitError, 429, "slow down")), 10.0, 0),
    ],
)
def test_a_hard_stop_or_spend_cap_marks_the_run_halted(messages: FakeMessages, hard_stop: float, halted: int) -> None:
    """A halted run is incomplete, so `anbud run` must not freeze it; a passing error doesn't halt it."""
    client = _client(messages, hard_stop=hard_stop)
    with pytest.raises((BudgetExceeded, ModelError)):
        _call(client)
    assert client.halted == halted


def test_shared_prefix_roles_must_send_identical_system_blocks() -> None:
    client = _client()
    _call(client, "map")
    with pytest.raises(PrefixDrift):
        _call(client, "lens", system=[{"type": "text", "text": "Something else."}])
    _call(client, "merge", system=[{"type": "text", "text": "Merge instructions."}])  # merge has its own prefix
    _call(client, "lens_generic_scope", system=[{"type": "text", "text": "Sonnet prefix."}])  # another model


def test_a_rejected_schema_is_classified_and_the_message_stays_off_logs() -> None:
    error = _status_error(anthropic.BadRequestError, 400, "Schema is too complex for compilation.")
    events: list = []
    client = _client(FakeMessages(error=error), events=events)
    with pytest.raises(ModelError) as caught:
        _call(client)
    assert caught.value.kind == "schema_rejected" and caught.value.status == 400
    assert "complex" in caught.value.message and "complex" not in str(caught.value)
    assert client.budget.reserved == pytest.approx(0) and client.budget.spent == 0  # an error response isn't billed
    assert [(e.kind, e.cost_usd) for e in events] == [("error", 0.0)]


@pytest.mark.parametrize(
    "messages",
    [
        FakeMessages(  # an overload once the stream is open: HTTP 200, then an error event
            mid_stream_error=anthropic.APIStatusError(
                "Overloaded",
                response=httpx2.Response(200, request=REQUEST),
                body={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
            )
        ),
        FakeMessages(error=anthropic.APIConnectionError(request=REQUEST)),
    ],
)
def test_a_failure_that_may_be_billed_keeps_the_reservation(messages: FakeMessages) -> None:
    client = _client(messages)
    with pytest.raises(ModelError):
        _call(client)
    assert client.budget.reserved == pytest.approx(0) and client.budget.spent > 0


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (_status_error(anthropic.EnforcedSpendLimitReachedError, 402, "limit"), "spend_cap"),
        (_status_error(anthropic.RateLimitError, 429, "slow down"), "rate_limited"),
        (_status_error(anthropic.AuthenticationError, 401, "bad key"), "auth"),
        (_status_error(anthropic.OverloadedError, 529, "overloaded"), "server"),
        (_status_error(anthropic.BadRequestError, 400, "messages: bad role"), "bad_request"),
        (
            _status_error(anthropic.BadRequestError, 400, "You have reached your specified workspace API usage limits"),
            "spend_cap",
        ),  # fmt: skip
        (_status_error(anthropic.RampLimitExceededError, 429, "ramp limit"), "rate_limited"),
        (anthropic.APIConnectionError(request=REQUEST), "connection"),
        (anthropic.APITimeoutError(request=REQUEST), "timeout"),
    ],
)
def test_errors_are_classified(error: Exception, kind: str) -> None:
    assert classify(error).kind == kind


def test_a_failed_token_count_reserves_nothing() -> None:
    fake = FakeMessages()
    fake.count_tokens = lambda **_: (_ for _ in ()).throw(_status_error(anthropic.AuthenticationError, 401, "x"))
    client = _client(fake)
    with pytest.raises(ModelError):
        _call(client)
    assert client.budget.reserved == 0 and client.budget.spent == 0 and fake.sent == []


@pytest.mark.parametrize(("field", "value"), [("effort", "extreme"), ("max_tokens", 0), ("role", "nobody")])
def test_bad_arguments_are_refused(field: str, value: Any) -> None:
    with pytest.raises((ValueError, KeyError)):
        _call(_client(), **{field: value})


def test_price_splits_cache_writes_and_bounds_the_worst_case() -> None:
    price = Price(input=4.0, output=20.0, cache_write_5m=5.0, cache_write_1h=8.0, cache_read=0.2)
    split_writes = CacheCreation(ephemeral_5m_input_tokens=100, ephemeral_1h_input_tokens=200)
    split = _usage(input_tokens=0, output_tokens=0, cache_creation_input_tokens=300, cache_creation=split_writes)
    assert price.cost(split) == pytest.approx((100 * 5.0 + 200 * 8.0) / 1_000_000)
    unsplit = _usage(input_tokens=0, output_tokens=0, cache_creation_input_tokens=300)
    assert price.cost(unsplit) == pytest.approx(300 * 8.0 / 1_000_000)
    worst = _usage(input_tokens=0, output_tokens=64, cache_creation_input_tokens=1000)
    assert price.max_cost(1000, 64) >= price.cost(worst)


def test_the_key_comes_from_the_keychain_and_is_never_echoed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: Any) -> SimpleNamespace:
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="sk-test-value\n", stderr="")

    monkeypatch.setattr(model_client.sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert model_client.keychain_key() == "sk-test-value"
    assert calls == [["security", "find-generic-password", "-s", "estim-anbud", "-a", "anbud-dev", "-w"]]
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=44, stdout="", stderr="none"))
    with pytest.raises(KeyMissing) as caught:
        model_client.keychain_key()
    assert "sk-" not in str(caught.value)


def _key_for_anbud_dev(account: str) -> str:
    assert account == "anbud-dev"  # the Keychain account is the workspace in config/providers.toml
    return "sk-test-value"


def test_compile_check_never_runs_in_ci(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("CI", "true")
    assert schema_check.main(["--compile"]) == schema_check.REFUSED


def test_compile_check_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(schema_check, "keychain_key", lambda **_: (_ for _ in ()).throw(KeyMissing("no entry")))
    assert schema_check.main(["--compile"]) == schema_check.NO_KEY


def test_compile_check_calls_each_model_once(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    fake = FakeMessages(_usage(input_tokens=4000, output_tokens=64))
    monkeypatch.setattr(schema_check, "keychain_key", _key_for_anbud_dev)
    monkeypatch.setattr(schema_check, "make_sdk_client", lambda config, key: SimpleNamespace(messages=fake))
    assert schema_check.main(["--compile"]) == 0
    assert sorted(p["model"] for p in fake.sent) == ["claude-opus-5-5", "claude-sonnet-5-5"]
    assert all(len(p["tools"]) == 10 and p["max_tokens"] == 64 for p in fake.sent)
    out = capsys.readouterr().out
    assert "accepted" in out and "sk-test-value" not in out


def test_compile_check_reports_a_rejected_schema(monkeypatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    error = _status_error(anthropic.BadRequestError, 400, "Schema is too complex for compilation.")
    monkeypatch.setattr(schema_check, "keychain_key", _key_for_anbud_dev)
    rejecting = SimpleNamespace(messages=FakeMessages(error=error))
    monkeypatch.setattr(schema_check, "make_sdk_client", lambda config, key: rejecting)
    assert schema_check.main(["--compile"]) == schema_check.FAILED
    assert "too complex" in capsys.readouterr().out


def test_the_sdk_retries_once_and_uses_the_configured_base_url() -> None:
    client = model_client.make_sdk_client(load_config(), "sk-test-value")
    assert client.max_retries == 1
    assert str(client.base_url).rstrip("/") == "https://api.anthropic.com"
