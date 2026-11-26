"""What the checks after the lenses share: a cheap role on the shared tool loop that answers in JSON text.

- The role is `lens_generic_scope` (Sonnet), which already sends the run's shared system blocks and tool list, so
  the checks read the Sonnet prefix cache the generic lens wrote and change neither the Opus prefix nor the tools
  array. The loop lets a check call only the read tools; it answers with one JSON object in its text, which code
  parses and checks (one repair turn).
- Every quote a check gives is verified against its block by code before anything uses it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from anbud.granskning.contracts import ToolCall
from anbud.granskning.corpus import Corpus
from anbud.granskning.loop import EFFORT, LoopOutcome, SubmitOutcome, run_tool_loop
from anbud.granskning.merge import STOP_ERRORS
from anbud.granskning.rank import document_names
from anbud.granskning.toolbox import Toolbox
from anbud.granskning.validate import quote_matches
from anbud.model_client import ModelClient, ModelError

CHECK_ROLE = "lens_generic_scope"
CHECK_TOOLS = frozenset({"search_blocks", "read_blocks", "outline", "calc_posts"})
CHECK_MAX_TOKENS = 8000

# A loop error that ends a check's remaining batches, and skips the checks when the lenses already ended on one: the
# stop errors by class name (a ModelError is logged by its kind), and the spend cap, which every later call would hit.
STOP_NAMES = frozenset(e.__name__ for e in STOP_ERRORS if e is not ModelError) | {"spend_cap"}

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class Answer(BaseModel):
    """A check's JSON answer: unknown keys are ignored, the rest is typed."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class Cite(Answer):
    block_id: str
    quote: str


def block_where(corpus: Corpus, block_id: str | None) -> str:
    """Where a block sits, for the report: file name, page or sheet and row."""
    block = corpus.get(block_id) if block_id else None
    if block is None:
        return ""
    names = document_names(corpus)
    parts = [names.get(block.doc_id, block.doc_id)]
    if block.page is not None:
        parts.append(f"s. {block.page}")
    if block.sheet is not None:
        parts.append(f"blad {block.sheet}")
    if block.row is not None:
        parts.append(f"rad {block.row}")
    return ", ".join(parts)


def cite_problem(corpus: Corpus, cite: Cite | None, what: str) -> str | None:
    """Why a citation fails, or None. Reasons name block ids only, never text."""
    if cite is None:
        return f"{what}: no citation"
    block = corpus.get(cite.block_id)
    if block is None:
        return f"{what}: block {cite.block_id} does not exist"
    if not quote_matches(cite.quote, block.text):
        return f"{what}: the quote is not in block {cite.block_id}"
    return None


def extract_json(text: str) -> Any | None:
    """The answer's JSON object: the last fenced block, else the outermost braces."""
    fenced = _FENCE.findall(text)
    candidates = list(reversed(fenced))
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None


def parse_answer[M: BaseModel](text: str, model: type[M]) -> tuple[M | None, list[str]]:
    data = extract_json(text)
    if data is None:
        return None, ["no JSON object found; answer with one JSON object in a ```json block"]
    try:
        return model.model_validate(data), []
    except ValidationError as exc:
        return None, [f"{'.'.join(map(str, e['loc'])) or 'answer'}: {e['msg']}" for e in exc.errors()][:20]


def repair_text(problems: list[str]) -> str:
    lines = "\n".join(f"- {p}" for p in problems[:40])
    return (
        "Your answer failed these checks. Answer again with the whole JSON object, the failed entries fixed or left "
        f"out. Quotes must be copied exactly from the block. This is your one repair.\n{lines}"
    )


@dataclass
class CheckRun:
    """One check's model loops (one per batch), for coverage and the run summary."""

    name: str
    outcomes: list[LoopOutcome] = field(default_factory=list)
    skipped: str | None = None  # why the check made no model call

    @property
    def cost_usd(self) -> float:
        return sum(o.cost_usd for o in self.outcomes)

    @property
    def turns(self) -> int:
        return sum(o.turns for o in self.outcomes)

    @property
    def tool_log(self) -> list[ToolCall]:
        return [c for o in self.outcomes for c in o.tool_log]

    @property
    def status(self) -> str:
        if self.skipped is not None:
            return "skipped"
        statuses = {o.status for o in self.outcomes}
        for worst in ("error", "truncated", "no_submit"):
            if worst in statuses:
                return worst
        return "submitted"

    @property
    def errors(self) -> list[str]:
        return [o.error for o in self.outcomes if o.error]


Handler = Callable[[str, list[ToolCall], bool], SubmitOutcome]


def run_check(
    client: ModelClient,
    *,
    step: str,
    system: list[dict[str, Any]],
    message: str,
    toolbox: Toolbox,
    handle: Handler,
    max_turns: int,
    max_usd: float,
    effort: str = EFFORT,
) -> LoopOutcome:
    def refuse_submit(raw: Any, log: list[ToolCall], final: bool) -> SubmitOutcome:  # never reached: not allowed
        return SubmitOutcome(final, "Answer in text, with one JSON object.")

    return run_tool_loop(
        client,
        role=CHECK_ROLE,
        step=step,
        system=system,
        first_message=message,
        toolbox=toolbox,
        submit_tool="submit_flags",
        on_submit=refuse_submit,
        max_turns=max_turns,
        max_usd=max_usd,
        max_tokens=CHECK_MAX_TOKENS,
        effort=effort,
        lens_id=step.split("/")[0],
        allowed=CHECK_TOOLS,
        on_text=handle,
    )
