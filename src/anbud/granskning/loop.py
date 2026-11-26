"""The tool loop every tool-using role runs: call, run the tools, feed the results back, until the role submits.

- Every call goes through `ModelClient.call` with the run's shared system blocks; the shared tool list comes from the
  client. A role is limited here, never by changing that list: a call to a tool the role (or the lens file) may not
  use gets an error `tool_result` from `tools.refusal` and still costs the turn.
- One cache breakpoint moves onto the newest user content (the latest tool results), so each turn reads the turns
  before it from the cache. With the system breakpoint that's two of the four the API allows.
- The submit tool is checked by a callback. If the check fails, the role gets the reasons and one repair turn; the
  second answer is final. A role that stops without submitting gets one nudge. Repair and nudge each get their own
  turn even past the turn cap.
- Turn and dollar caps end the loop as "truncated". The turn before a cap carries a note telling the role to submit.
- Every tool use is recorded as a `ToolCall`; coverage is computed from these alone.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from anbud.granskning.contracts import LensResult, ToolCall, fingerprint
from anbud.granskning.merge import code_merge
from anbud.granskning.toolbox import Toolbox
from anbud.model_client import BudgetExceeded, ModelClient, ModelError, PrefixDrift, ReplayMiss, RunStopped
from anbud.models import Flag
from anbud.tools import refusal

CACHE_5M = {"type": "ephemeral"}
LENS_MAX_TOKENS = 16_000
EFFORT = "high"

Validator = Callable[[list[dict], list[ToolCall]], tuple[list[Flag], list[dict]]]
STOP_ERRORS = (ModelError, BudgetExceeded, RunStopped, ReplayMiss, PrefixDrift)


@dataclass
class SubmitOutcome:
    done: bool  # True ends the loop; False sends `reply` back as an error and grants the repair turn
    reply: str


SubmitHandler = Callable[[Any, list[ToolCall], bool], SubmitOutcome]  # (input, tool log, final attempt)


@dataclass
class LoopOutcome:
    status: str  # submitted, truncated, no_submit, error
    turns: int = 0
    cost_usd: float = 0.0
    tool_log: list[ToolCall] = field(default_factory=list)
    error: str | None = None  # an exception class or ModelError kind, never an API message
    first_usage: Any = None  # usage of the first response, for the prefix size


def _plain(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return block
    return block.model_dump(mode="json", exclude_none=True)


def with_breakpoint(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A copy of the messages with one 5-minute cache breakpoint on the last block of the last message."""
    out = [dict(m) for m in messages]
    last = out[-1]
    content = last["content"]
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    content = [dict(b) for b in content]
    content[-1]["cache_control"] = dict(CACHE_5M)
    last["content"] = content
    return out


def _tool_result(use_id: str, content: str | list[dict[str, Any]], is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "tool_result", "tool_use_id": use_id, "content": content}
    if is_error:
        result["is_error"] = True
    return result


def run_tool_loop(
    client: ModelClient,
    *,
    role: str,
    step: str,
    system: list[dict[str, Any]],
    first_message: str,
    toolbox: Toolbox,
    submit_tool: str,
    on_submit: SubmitHandler,
    max_turns: int,
    max_usd: float,
    max_tokens: int = LENS_MAX_TOKENS,
    effort: str = EFFORT,
    lens_id: str | None = None,
    allowed: frozenset[str] | None = None,
    on_text: SubmitHandler | None = None,
) -> LoopOutcome:
    """With `on_text`, the role answers in text instead of a submit tool: a turn without a tool call is its answer,
    checked by `on_text` like a submission (one repair turn), and the closing notes ask for the answer."""
    finish = f"Call {submit_tool} in it" if on_text is None else "Give your final answer in it"
    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"type": "text", "text": first_message}]}]
    out = LoopOutcome(status="truncated")
    extra = 0  # turns granted past the cap for a repair or a nudge
    repaired = nudged = False
    last_cost = 0.0
    while True:
        if out.turns >= max_turns + extra or out.cost_usd >= max_usd:
            out.status = "truncated"
            return out
        try:
            result = client.call(
                role=role,
                step=f"{step}/t{out.turns + 1}",
                system=system,
                messages=with_breakpoint(messages),
                max_tokens=max_tokens,
                effort=effort,
            )
        except STOP_ERRORS as exc:
            out.status = "error"
            out.error = exc.kind if isinstance(exc, ModelError) else type(exc).__name__
            return out
        out.turns += 1
        out.cost_usd += result.cost_usd
        last_cost = result.cost_usd
        if out.first_usage is None:
            out.first_usage = result.message.usage
        content = [_plain(b) for b in result.message.content]
        messages.append({"role": "assistant", "content": content})
        uses = [b for b in content if b.get("type") == "tool_use"]
        if not uses:
            if result.message.stop_reason == "refusal":
                out.status, out.error = "error", "refusal"
                return out
            if on_text is not None:
                text = "\n".join(str(b.get("text", "")) for b in content if b.get("type") == "text")
                answer = on_text(text, out.tool_log, repaired)
                if answer.done:
                    out.status = "submitted"
                    return out
                repaired = True
                extra += 1
                messages.append({"role": "user", "content": [{"type": "text", "text": answer.reply}]})
                continue
            if nudged:
                out.status = "no_submit"
                return out
            nudged = True
            extra += 1
            note = f"You stopped without calling {submit_tool}. Call {submit_tool} now with what you have found."
            messages.append({"role": "user", "content": [{"type": "text", "text": note}]})
            continue
        results: list[dict[str, Any]] = []
        submitted = False
        for use in uses:
            name, raw, use_id = use.get("name", ""), use.get("input"), use.get("id", "")
            call = {"role": role, "lens_id": lens_id, "turn": out.turns, "tool": name}
            call["input"] = raw if isinstance(raw, dict) else {"_raw": raw}
            reason = refusal(role, name)
            if reason is None and allowed is not None and name not in allowed:
                reason = f"{name} is not available to this lens"
            if reason is not None:
                out.tool_log.append(ToolCall(**call, refused=reason))
                results.append(_tool_result(use_id, reason, is_error=True))
                continue
            if name == submit_tool:
                if submitted:
                    results.append(_tool_result(use_id, "Already submitted.", is_error=True))
                    continue
                outcome = on_submit(raw, out.tool_log, repaired)
                out.tool_log.append(ToolCall(**call, error=None if outcome.done else "validation"))
                if outcome.done:
                    submitted = True
                    results.append(_tool_result(use_id, outcome.reply))
                else:
                    repaired = True
                    extra += 1
                    results.append(_tool_result(use_id, outcome.reply, is_error=True))
                continue
            if name.startswith("submit_"):
                reason = f"{name} is not this task's submit tool; call {submit_tool}"
                out.tool_log.append(ToolCall(**call, refused=reason))
                results.append(_tool_result(use_id, reason, is_error=True))
                continue
            output = toolbox.run(name, raw)
            out.tool_log.append(ToolCall(**call, error=output.error, result_ids=output.result_ids))
            results.append(_tool_result(use_id, output.content, is_error=output.error is not None))
        if submitted:
            out.status = "submitted"
            return out
        remaining = max_turns + extra - out.turns
        if remaining <= 1 or out.cost_usd + last_cost >= max_usd:
            note = f"Your next turn is your last. {finish} with everything you have found so far."
            results.append({"type": "text", "text": note})
        messages.append({"role": "user", "content": results})


# Lenses


def pydantic_validator(raw_flags: list[dict], tool_log: list[ToolCall]) -> tuple[list[Flag], list[dict]]:
    """The default check: each flag must parse as a `Flag`. The real validator (citations, quotes, numbers, absence
    searches) replaces this."""
    flags: list[Flag] = []
    rejected: list[dict] = []
    for index, raw in enumerate(raw_flags):
        try:
            flags.append(Flag.model_validate(raw))
        except ValidationError as exc:
            reasons = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'flag'}: {e['msg']}" for e in exc.errors())
            rejected.append({"index": index, "flag": raw, "reason": reasons})
    return flags, rejected


class FlagSubmission:
    """Collects a lens's flags across its submit and its one repair.

    Every reject is recorded, from both attempts, with the attempt number. Two findings with the same fingerprint
    (same cited blocks, same claim type) can't both stand: on the first attempt neither is kept and the lens is asked
    to combine them; on the final attempt code combines them, keeping both claims, and records the later one's money
    basis as a reject so it isn't lost. An exact repeat of a kept finding is ignored.
    """

    def __init__(self, lens_id: str, validator: Validator) -> None:
        self.lens_id = lens_id
        self.validator = validator
        self.rejected: list[dict] = []
        self.attempts = 0
        self._kept: dict[str, Flag] = {}

    @property
    def flags(self) -> list[Flag]:
        return list(self._kept.values())

    @staticmethod
    def _index(items: list[Any], flag: Flag) -> int | None:
        return next(
            (i for i, item in enumerate(items) if isinstance(item, dict) and item.get("claim") == flag.claim), None
        )

    def __call__(self, raw: Any, tool_log: list[ToolCall], final: bool) -> SubmitOutcome:
        self.attempts += 1
        items = raw.get("flags") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            self.rejected.append(
                {"index": None, "flag": raw, "reason": "input has no flags list", "attempt": self.attempts}
            )
            if final:
                return SubmitOutcome(True, "Received, but the input had no flags list.")
            return SubmitOutcome(False, 'submit_flags takes {"flags": [...]}, a list of findings (empty if none).')
        items = [
            dict({k: v for k, v in i.items() if k != "check_key"}, lens_id=self.lens_id) if isinstance(i, dict) else i
            for i in items
        ]
        accepted, bad = self.validator(items, tool_log)
        bad = list(bad)
        for flag in accepted:
            key = fingerprint(flag)
            kept = self._kept.get(key)
            if kept is None:
                self._kept[key] = flag
            elif kept == flag:
                continue
            elif final:
                self._kept[key] = code_merge([kept, flag])
                reason = "same cited blocks and claim type as another finding; merged by code, its money basis unused"
                bad.append({"index": self._index(items, flag), "flag": flag.model_dump(mode="json"), "reason": reason})
            else:
                del self._kept[key]
                reason = (
                    "this finding and another cite the same blocks with the same claim_type, so neither is kept; "
                    "submit them again as one finding with one claim and one money basis, or cite the blocks that "
                    "set them apart"
                )
                for duplicate in (kept, flag):
                    dumped = duplicate.model_dump(mode="json")
                    bad.append({"index": self._index(items, duplicate), "flag": dumped, "reason": reason})
        self.rejected.extend(dict(item, attempt=self.attempts) for item in bad)
        if not bad or final:
            failed = f" {len(bad)} failed checks." if bad else ""
            return SubmitOutcome(True, f"{len(self._kept)} findings kept." + failed)
        lines = [
            f"{len(self._kept)} findings passed and are kept. {len(bad)} failed the checks below. Call submit_flags "
            "once more with only those findings, corrected; leave out any you can't support. This is your one repair."
        ]
        for item in bad:
            where = f"finding {item['index']}" if item.get("index") is not None else "a finding"
            lines.append(f"- {where}: {item.get('reason')}")
        return SubmitOutcome(False, "\n".join(lines))


def run_lens(
    client: ModelClient,
    *,
    lens_id: str,
    role: str,
    system: list[dict[str, Any]],
    first_message: str,
    toolbox: Toolbox,
    max_turns: int,
    max_usd: float,
    validator: Validator = pydantic_validator,
    allowed: frozenset[str] | None = None,
    generic: bool = False,
    max_tokens: int = LENS_MAX_TOKENS,
    effort: str = EFFORT,
    on_outcome: Callable[[LoopOutcome], None] | None = None,
) -> LensResult:
    """`on_outcome` sees the loop's whole outcome; the result carries its error kind too."""
    submission = FlagSubmission(lens_id, validator)
    outcome = run_tool_loop(
        client,
        role=role,
        step=f"lens:{lens_id}",
        system=system,
        first_message=first_message,
        toolbox=toolbox,
        submit_tool="submit_flags",
        on_submit=submission,
        max_turns=max_turns,
        max_usd=max_usd,
        max_tokens=max_tokens,
        effort=effort,
        lens_id=lens_id,
        allowed=allowed,
    )
    if on_outcome is not None:
        on_outcome(outcome)
    return LensResult(
        lens_id=lens_id,
        model=client.config.model_for(role),
        status=outcome.status,
        flags=submission.flags,
        rejected=[_jsonable(r) for r in submission.rejected],
        tool_log=outcome.tool_log,
        turns=outcome.turns,
        cost_usd=outcome.cost_usd,
        generic=generic,
        error=outcome.error,
    )


def _jsonable(value: dict) -> dict:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))
