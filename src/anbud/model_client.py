"""ModelClient v0: the only way the harness calls a model.

- Plain Messages API, always streamed, with an explicit `max_tokens` and effort. Model ids come from
  config/providers.toml by role. No sampling parameters, no thinking settings (adaptive is the default and the only
  mode on the pinned models), no fallback to another model: a run must be reproducible on the model it names.
- Tool-using roles send the shared tool list with `tool_choice` auto (forced tool choice returns 400 on these models),
  or their own list if they declare one (Kalkyl's Sonnet roles). Roles marked `shared_prefix` must send
  byte-identical system blocks per model (per role for an own list), so the cache is written once a run.
- Every call reserves its worst-case cost from the run's budget before it is sent, and settles the real cost after.
  A call that would cross the hard stop is refused unsent.
- Events carry ids, sizes, timings and costs, never prompt text or model output. Error messages from the API stay on
  the exception (`.message`) and never go into an event.
- The key is read from the macOS Keychain at run time and is never printed or logged.
- With a RunStore, every finished call is stored (request, response, step-cache entry, event), and a request already
  in the step cache is answered from it at $0 without counting tokens or reserving budget. That gives resume after a
  crash and free replay. Without an SDK client (replay), a request not in the cache is refused, never sent.
- A stop marker (`anbud stop`) is checked before every call.
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
from typing import Any, NoReturn

from anbud.config import Config
from anbud.models import Event
from anbud.store import RunStore, canonical
from anbud.tools import ROLES, own_tools, shared_tools

EFFORTS = ("low", "medium", "high", "xhigh", "max")
# A call that fails on one of these kinds is tried again after each delay in turn (seconds), on top of the SDK's own
# retry: a 429, a 529 or 5xx (one mid-stream too, which the SDK never retries), a dropped connection, a timeout. Each
# attempt reserves against the hard stop like any call, so a retry the stop refuses is skipped or halts as usual.
TRANSIENT = frozenset({"rate_limited", "server", "connection", "timeout"})
RETRY_DELAYS = (5.0, 20.0)
KEYCHAIN_SERVICE = "estim-anbud"
KEYCHAIN_ACCOUNT = "anbud-dev"
KEYCHAIN_TIMEOUT_S = 10.0  # the `security` read; a locked Keychain or a prompt fails fast instead of hanging


class BudgetExceeded(RuntimeError):
    pass


class PrefixDrift(RuntimeError):
    pass


class KeyMissing(RuntimeError):
    pass


class RunStopped(RuntimeError):
    pass


class ReplayMiss(RuntimeError):
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

    def __init__(self, hard_stop_usd: float, ceiling_usd: float | None = None) -> None:
        self.hard_stop = hard_stop_usd
        self.ceiling = hard_stop_usd if ceiling_usd is None else ceiling_usd  # set_stop never goes above it
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

    def set_stop(self, usd: float) -> float:
        """Moves the stop when the run learns its size (`anbud.stops`), never above the ceiling set at the start."""
        with self._lock:
            self.hard_stop = min(usd, self.ceiling)
            return self.hard_stop

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
    cached: bool = False
    paid_usd: float | None = None  # a cache hit: what the call cost when it was first made (its step record)

    @property
    def stop_reason(self) -> str | None:
        return self.message.stop_reason

    @property
    def loop_cost_usd(self) -> float:
        """The call's cost for a tool loop's own caps: what it cost when it was made, a cache hit included. A replay
        or a resume then decides each turn as the paid run did, while the budget charges a cache hit $0."""
        return self.cost_usd if self.paid_usd is None else self.paid_usd


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
        if "usage limit" in lowered or "credit balance" in lowered:  # a workspace cap or an empty balance: a 400
            kind = "spend_cap"  # pause the run, never retry it
        elif "schema" in lowered:
            kind = "schema_rejected"
        else:
            kind = "bad_request"
        return ModelError(kind, status, request_id, message)
    return ModelError("status", status, request_id, message)


def keychain_key(service: str = KEYCHAIN_SERVICE, account: str = KEYCHAIN_ACCOUNT) -> str:
    """The model key, read at run time. Never print, log or store the return value. A locked Keychain or one waiting
    at a prompt fails after `KEYCHAIN_TIMEOUT_S` instead of hanging the run's start."""
    if sys.platform != "darwin":
        raise KeyMissing("the model key is read from the macOS Keychain, which isn't available here")
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            check=False,
            timeout=KEYCHAIN_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:  # `from None`: the expired call holds what the child printed
        raise KeyMissing(
            f"the Keychain read timed out after {KEYCHAIN_TIMEOUT_S:g} s (locked, or waiting at a prompt)"
        ) from None
    except OSError as exc:
        raise KeyMissing(f"the Keychain can't be read ({type(exc).__name__})") from None
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
        store: RunStore | None = None,
        salt: str | None = None,
        degrade_roles: frozenset[str] = frozenset(),
    ) -> None:
        """`degrade_roles`: roles whose call the hard stop refuses is skipped, not a halt (`_refuse`). Granskning
        passes its lenses, merge and verifier: a run that reached them finishes with what it has. Kalkyl passes none:
        there a refused call leaves the calc incomplete, so the run halts."""
        if store is not None and store.company_id != company_id:
            raise ValueError("the store belongs to another company")
        self.store = store
        self.salt = store.open_run(run_id, salt) if store is not None else salt
        if store is not None:
            budget.settle(0.0, store.run_spend(run_id))  # a resumed run keeps counting against the same hard stop
        self.persist_failures = 0  # store writes that failed; the caller should stop the run if this isn't 0
        self.replay_misses = 0  # replayed requests not in the step cache; a replay with any is incomplete
        self.halted = 0  # calls refused by a stop marker, the hard stop or the spend cap; a run with any is incomplete
        self.degrade_roles = degrade_roles
        self.budget_refused = 0  # calls of `degrade_roles` the hard stop refused: the run goes on without them
        self.refuse_roles: set[str] = set()  # roles a paid run refuses from now on, as at the hard stop (`refuse_role`)
        self.retries = 0  # attempts made again after a transient error (`TRANSIENT`); a replay counts the recorded ones
        self.failures: dict[str, int] = {}  # calls that failed for good, after their retries, by error kind
        self.answered: dict[str, int] = {}  # calls answered, by the model or from the step cache, by step
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
        if ROLES[role].own_tools:
            params["tools"] = own_tools(role)
            params["tool_choice"] = {"type": "auto"}
        elif ROLES[role].tools:
            params["tools"] = shared_tools()
            params["tool_choice"] = {"type": "auto"}
        return params

    def _check_prefix(self, role: str, params: dict[str, Any]) -> None:
        if not ROLES[role].shared_prefix:
            return
        digest = _digest(params.get("tools"), params["system"])
        group = f"role:{role}" if ROLES[role].own_tools else params["model"]  # own lists never share the model's prefix
        with self._lock:
            first = self._prefixes.setdefault(group, digest)
        if first != digest:
            raise PrefixDrift(f"{role} would send a different tools or system prefix than earlier calls on its model")

    def _emit(self, step: str, kind: str, **fields: Any) -> None:
        event = Event(
            run_id=self.run_id, company_id=self.company_id, step=step, kind=kind, at=datetime.now(UTC), **fields
        )
        if self.store is not None:
            try:
                self.store.append_event(event)
            except OSError:  # never let a full or read-only disk hide the call's own result or error
                self.persist_failures += 1
        if self.on_event:
            self.on_event(event)

    def _cached(self, step: str, model: str, key: str) -> CallResult | None:
        record = self.store.step_get(key) if self.store is not None else None
        if record is None:
            return None
        if record.get("refused"):  # the hard stop refused this request when it was first made
            if self.sdk is None:
                self._count(None, record.get("retries", 0))
                self._refuse(step, None, "the hard stop refused this call when it was made")  # a replay refuses it too
            return None  # a paid run tries it again
        if record.get("failed"):  # the request failed when it was first made, its retries too
            if self.sdk is None:  # a replay fails it the same way, so it loses the same work
                self._count(record["failed"], record.get("retries", 0))
                self._emit(step, "error", cost_usd=0.0)
                raise ModelError(record["failed"], None, None, "this call failed when it was made")
            return None
        if self.sdk is None:
            self._count(None, record.get("retries", 0))
        import anthropic

        message = anthropic.types.Message.model_validate_json(self.store.get_blob(record["response"]))
        usage = message.usage
        self._emit(
            step,
            "cache_hit",
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=0.0,
            blob=record["response"],
            request_blob=record["request"],
        )
        paid = float(record.get("cost_usd") or 0.0)
        return CallResult(message, model, 0.0, 0, record.get("request_id"), cached=True, paid_usd=paid)

    def call(
        self,
        *,
        role: str,
        step: str,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int,
        effort: str,
        sample: int = 0,
    ) -> CallResult:
        """`sample` asks for a fresh answer to a request already made in this run (a second verifier vote)."""
        params = self.request(role=role, system=system, messages=messages, max_tokens=max_tokens, effort=effort)
        self._check_prefix(role, params)
        model = params["model"]
        if self.store is not None and self.store.stop_requested(self.run_id):
            self._halt()
            raise RunStopped(f"run {self.run_id} has a stop marker")
        key = self.store.step_key(self.salt, params, step, sample) if self.store is not None else None
        if key is not None and (hit := self._cached(step, model, key)) is not None:
            self._answer(step)
            return hit
        if self.sdk is None:
            with self._lock:
                self.replay_misses += 1
            raise ReplayMiss("this request isn't in the step cache, and replay never calls a model")
        for retried, delay in enumerate((*RETRY_DELAYS, None)):
            try:
                result = self._send(role, step, params, key, max_tokens, retried)
                self._answer(step)
                return result
            except ModelError as error:
                if error.kind not in TRANSIENT or delay is None:
                    self._count(error.kind, 0)
                    self._record_failure(key, error, retried)
                    raise
            self._count(None, 1)
            time.sleep(delay)
            if self.store is not None and self.store.stop_requested(self.run_id):
                self._halt()
                raise RunStopped(f"run {self.run_id} has a stop marker")
        raise AssertionError("unreachable")

    def _send(
        self, role: str, step: str, params: dict[str, Any], key: str | None, max_tokens: int, retried: int = 0
    ) -> CallResult:
        """One attempt: the token count, the reservation, the stream, the settle, the step record and the event.
        `retried`: the attempts before this one, kept in the step record so a replay counts them too."""
        model = params["model"]
        if role in self.refuse_roles and role in self.degrade_roles:
            self._refuse(step, key, f"{role} calls stopped at the hard stop", retried)
        price = self.config.price_for(model)
        count_params = {k: v for k, v in params.items() if k != "max_tokens"}
        try:
            input_tokens = self.sdk.messages.count_tokens(**count_params).input_tokens
        except Exception as exc:  # noqa: BLE001 - mapped to ModelError below; any other failure is a bug and re-raised
            raise self._failed(exc) from exc
        try:
            reserved = self.budget.reserve(price.max_cost(input_tokens, max_tokens))
        except BudgetExceeded as exc:
            if role in self.degrade_roles:
                self._refuse(step, key, str(exc), retried)
            self._halt()
            raise
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
            raise self._failed(exc) from exc
        cost = price.cost(message.usage)
        self.budget.settle(reserved, cost)
        usage = message.usage
        duration = self._ms(start)
        blobs: dict[str, Any] = {}
        response_bytes = None
        if self.store is not None and key is not None:
            response_bytes = message.model_dump_json().encode()
            try:
                blobs = {
                    "request_blob": self.store.put_blob(canonical(params)),
                    "blob": self.store.put_blob(response_bytes),
                }
                # Only a finished stream reaches here, so only complete responses are cached.
                record = {"request": blobs["request_blob"], "response": blobs["blob"], "model": model}
                if retried:
                    record["retries"] = retried
                self.store.step_put(key, {**record, "request_id": request_id, "cost_usd": cost})
            except OSError:  # the paid response still goes back to the caller, and the cost into the event
                self.persist_failures += 1
                blobs = {}
        self._emit(
            step,
            "model_call",
            duration_ms=duration,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_input_tokens,
            cache_write_tokens=usage.cache_creation_input_tokens,
            cost_usd=cost,
            size_bytes=len(response_bytes) if response_bytes is not None else None,
            **blobs,
        )
        return CallResult(message, model, cost, duration, request_id)

    def _answer(self, step: str) -> None:
        with self._lock:
            self.answered[step] = self.answered.get(step, 0) + 1

    def unanswered(self, prefix: str) -> bool:
        """Calls failed for good, of any kind, and no call whose step starts with `prefix` was answered: the run did
        none of that work (a dead key, an empty balance, the network or the API down past the retries), and must not
        count as finished. By step, not role: Granskning's checks share a lens role. A run that failed nothing (a stop
        marker, the hard stop) is left to its own exit."""
        with self._lock:
            return bool(self.failures) and not any(s.startswith(prefix) for s in self.answered)

    def _count(self, failed: str | None, retries: int) -> None:
        with self._lock:
            self.retries += retries
            if failed is not None:
                self.failures[failed] = self.failures.get(failed, 0) + 1

    def _record_failure(self, key: str | None, error: ModelError, retried: int) -> None:
        """A call that failed for good, recorded under its step key so a $0 rebuild fails it too and loses the same
        work; a paid resume tries it again. Not the spend cap: it halts the run, and a halted run is resumed, never
        rebuilt."""
        if self.store is None or key is None or error.kind == "spend_cap":
            return
        try:
            self.store.step_put(key, {"failed": error.kind, **({"retries": retried} if retried else {})})
        except OSError:
            self.persist_failures += 1

    def _failed(self, exc: Exception) -> ModelError:
        """The mapped error; the workspace's spend cap halts the run like its own hard stop."""
        error = self._mapped(exc)
        if error.kind == "spend_cap":
            self._halt()
        return error

    def _halt(self) -> None:
        with self._lock:
            self.halted += 1

    def refuse_role(self, role: str) -> None:
        """From now on, a paid run refuses `role`'s calls as the hard stop would (the verifier, once one of its calls
        was refused). A replay follows each request's own record instead, so the same calls are refused in any order."""
        with self._lock:
            self.refuse_roles.add(role)

    def _refuse(self, step: str, key: str | None, why: str, retried: int = 0) -> NoReturn:
        """A call of a degrade role refused at the hard stop: counted, logged and recorded under its step key, so a
        rebuild refuses the same request, then raised. Never a halt: the caller goes on without it."""
        with self._lock:
            self.budget_refused += 1
        if self.store is not None and key is not None:
            try:
                self.store.step_put(key, {"refused": "budget", **({"retries": retried} if retried else {})})
            except OSError:
                self.persist_failures += 1
        self._emit(step, "error", cost_usd=0.0)
        raise BudgetExceeded(why)

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
        """The SDK's errors, and a transport error it lets through: a stream that drops after it opened (a protocol,
        read or write error, a read timeout) surfaces as httpx's own error, not the SDK's. Anything else is a bug."""
        import anthropic
        import httpx2

        if isinstance(exc, anthropic.APIError):
            return classify(exc)
        if isinstance(exc, httpx2.TimeoutException):
            return ModelError("timeout", None, None, type(exc).__name__)
        if isinstance(exc, httpx2.TransportError):
            return ModelError("connection", None, None, type(exc).__name__)
        raise exc
