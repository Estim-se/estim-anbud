"""ModelClient v0: the only way the harness calls a model.

- Plain Messages API, always streamed, with an explicit `max_tokens` and effort. Model ids come from
  config/providers.toml by role. No sampling parameters, no thinking settings (adaptive is the default and the only
  mode on the pinned models), no fallback to another model: a run must be reproducible on the model it names.
- Tool-using roles send the shared tool list with `tool_choice` auto (forced tool choice returns 400 on these models).
  Roles marked `shared_prefix` must send byte-identical system blocks per model, so the cache is written once a run.
- Every call reserves its worst-case cost from the run's budget before it is sent, and settles the real cost after.
  A call that would cross the hard stop is refused unsent.
- Events carry ids, sizes, timings and costs, never prompt text or model output. Error messages from the API stay on
  the exception (`.message`) and never go into an event.
- The key is read from the macOS Keychain at run time and is never printed or logged.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from anbud.config import Config
from anbud.models import Event
from anbud.tools import ROLES, shared_tools

EFFORTS = ("low", "medium", "high", "xhigh", "max")
KEYCHAIN_SERVICE = "estim-anbud"
KEYCHAIN_ACCOUNT = "anbud-dev"


class BudgetExceeded(RuntimeError):
    pass


class PrefixDrift(RuntimeError):
    pass


class KeyMissing(RuntimeError):
    pass


class ModelError(RuntimeError):
    """A failed call. `kind` is one of: schema_rejected, bad_request, too_large, auth, spend_cap, rate_limited, server,
    timeout, connection, status. `str()` never includes the API's message."""

    def __init__(self, kind: str, status: int | None, request_id: str | None, message: str) -> None:
        super().__init__(kind)
        self.kind = kind
        self.status = status
        self.request_id = request_id
        self.message = message

    def __str__(self) -> str:
        status = f"HTTP {self.status}" if self.status else "no response"
        return f"{self.kind} ({status}, request {self.request_id or 'unknown'})"


class Budget:
    """A run's hard stop in USD. Thread-safe, for parallel lenses."""

    def __init__(self, hard_stop_usd: float) -> None:
        self.hard_stop = hard_stop_usd
        self.spent = 0.0
        self.reserved = 0.0
        self._lock = threading.Lock()

    def reserve(self, usd: float) -> float:
        with self._lock:
            if self.spent + self.reserved + usd > self.hard_stop:
                raise BudgetExceeded(
                    f"a call that may cost ${usd:.4f} would pass the ${self.hard_stop:.2f} hard stop "
                    f"(spent ${self.spent:.4f}, reserved ${self.reserved:.4f})"
                )
            self.reserved += usd
            return usd

    def settle(self, reserved_usd: float, actual_usd: float) -> None:
        with self._lock:
            self.reserved -= reserved_usd
            self.spent += actual_usd


@dataclass(frozen=True)
class CallResult:
    message: Any  # anthropic.types.Message
    model: str
    cost_usd: float
    duration_ms: int
    request_id: str | None

    @property
    def stop_reason(self) -> str | None:
        return self.message.stop_reason


def classify(exc: Exception) -> ModelError:
    import anthropic

    status = getattr(exc, "status_code", None)
    request_id = getattr(exc, "request_id", None)
    message = getattr(exc, "message", None) or type(exc).__name__
    kinds: list[tuple[type[Exception], str]] = [
        (anthropic.EnforcedSpendLimitReachedError, "spend_cap"),
        (anthropic.RampLimitExceededError, "rate_limited"),
        (anthropic.AuthenticationError, "auth"),
        (anthropic.PermissionDeniedError, "auth"),
        (anthropic.RateLimitError, "rate_limited"),
        (anthropic.RequestTooLargeError, "too_large"),
        (anthropic.ContentTooLargeError, "too_large"),
        (anthropic.OverloadedError, "server"),
        (anthropic.InternalServerError, "server"),
        (anthropic.ServiceUnavailableError, "server"),
        (anthropic.GatewayTimeoutError, "server"),
        (anthropic.APITimeoutError, "timeout"),
        (anthropic.APIConnectionError, "connection"),
    ]
    for cls, kind in kinds:
        if isinstance(exc, cls):
            return ModelError(kind, status, request_id, message)
    if isinstance(exc, anthropic.BadRequestError):
        lowered = message.lower()
        if "usage limit" in lowered:  # a workspace cap arrives as a 400: pause the run, never retry it
            kind = "spend_cap"
        elif "schema" in lowered:
            kind = "schema_rejected"
        else:
            kind = "bad_request"
        return ModelError(kind, status, request_id, message)
    return ModelError("status", status, request_id, message)


def keychain_key(service: str = KEYCHAIN_SERVICE, account: str = KEYCHAIN_ACCOUNT) -> str:
    """The model key, read at run time. Never print, log or store the return value."""
    if sys.platform != "darwin":
        raise KeyMissing("the model key is read from the macOS Keychain, which isn't available here")
    proc = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    key = proc.stdout.strip()
    if proc.returncode != 0 or not key:
        raise KeyMissing(f"no Keychain entry for service {service}, account {account}")
    return key


def make_sdk_client(config: Config, api_key: str) -> Any:
    import anthropic

    # The base URL comes from config, so ANTHROPIC_BASE_URL can't redirect the key elsewhere. The SDK retries once;
    # further retries go through the harness's step cache, so a resumed run never pays twice for a finished call.
    return anthropic.Anthropic(api_key=api_key, base_url=config.api_base, max_retries=1, timeout=600.0)


def _digest(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class ModelClient:
    def __init__(
        self,
        config: Config,
        sdk: Any,
        budget: Budget,
        *,
        run_id: str,
        company_id: str,
        on_event: Callable[[Event], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.sdk = sdk
        self.budget = budget
        self.run_id = run_id
        self.company_id = company_id
        self.on_event = on_event
        self.clock = clock
        self._prefixes: dict[str, str] = {}
        self._lock = threading.Lock()

    def request(
        self, *, role: str, system: list[dict[str, Any]], messages: list[dict[str, Any]], max_tokens: int, effort: str
    ) -> dict[str, Any]:
        """The exact parameters a call sends. Public so tests and replays can check them."""
        if role not in ROLES:
            raise KeyError(f"unknown role: {role}")
        if effort not in EFFORTS:
            raise ValueError(f"effort must be one of {EFFORTS}")
        if max_tokens < 1:
            raise ValueError("max_tokens must be at least 1")
        params: dict[str, Any] = {
            "model": self.config.model_for(role),
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "output_config": {"effort": effort},
        }
        if ROLES[role].tools:
            params["tools"] = shared_tools()
            params["tool_choice"] = {"type": "auto"}
        return params

    def _check_prefix(self, role: str, params: dict[str, Any]) -> None:
        if not ROLES[role].shared_prefix:
            return
        digest = _digest(params.get("tools"), params["system"])
        with self._lock:
            first = self._prefixes.setdefault(params["model"], digest)
        if first != digest:
            raise PrefixDrift(f"{role} would send a different tools or system prefix than earlier calls on its model")

    def _emit(self, step: str, kind: str, **fields: Any) -> None:
        if self.on_event:
            self.on_event(
                Event(
                    run_id=self.run_id, company_id=self.company_id, step=step, kind=kind, at=datetime.now(UTC), **fields
                )
            )

    def call(
        self,
        *,
        role: str,
        step: str,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int,
        effort: str,
    ) -> CallResult:
        params = self.request(role=role, system=system, messages=messages, max_tokens=max_tokens, effort=effort)
        self._check_prefix(role, params)
        model = params["model"]
        price = self.config.price_for(model)
        count_params = {k: v for k, v in params.items() if k != "max_tokens"}
        try:
            input_tokens = self.sdk.messages.count_tokens(**count_params).input_tokens
        except Exception as exc:  # noqa: BLE001 - mapped to ModelError below; any other failure is a bug and re-raised
            raise self._mapped(exc) from exc
        reserved = self.budget.reserve(price.max_cost(input_tokens, max_tokens))
        start = self.clock()
        opened = False
        request_id = None
        try:
            with self.sdk.messages.stream(**params) as stream:  # the request is sent here
                opened = True
                request_id = getattr(stream, "request_id", None)  # streamed messages don't carry it themselves
                message = stream.get_final_message()
        except Exception as exc:  # noqa: BLE001
            charged = 0.0 if self._refused_unbilled(exc, opened) else reserved
            self.budget.settle(reserved, charged)
            self._emit(step, "error", duration_ms=self._ms(start), cost_usd=charged)
            raise self._mapped(exc) from exc
        cost = price.cost(message.usage)
        self.budget.settle(reserved, cost)
        usage = message.usage
        duration = self._ms(start)
        self._emit(
            step,
            "model_call",
            duration_ms=duration,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_input_tokens,
            cache_write_tokens=usage.cache_creation_input_tokens,
            cost_usd=cost,
        )
        return CallResult(message, model, cost, duration, request_id)

    def _ms(self, start: float) -> int:
        return int((self.clock() - start) * 1000)

    @staticmethod
    def _refused_unbilled(exc: Exception, opened: bool) -> bool:
        """An error response before the stream opened (400, 401, 429, 529...) is not billed. A connection failure or a
        failure mid-stream may be, so those keep the whole reservation as spent."""
        import anthropic

        return not opened and isinstance(exc, anthropic.APIStatusError)

    @staticmethod
    def _mapped(exc: Exception) -> ModelError:
        import anthropic

        if isinstance(exc, anthropic.APIError):
            return classify(exc)
        raise exc
