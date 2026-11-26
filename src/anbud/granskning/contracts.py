"""The contracts between Granskning's stages. Stages are built in parallel against these; change them only with every
stage in view.

Flow: `Corpus` (from prep) -> map -> lenses, each returning a `LensResult` -> checks, merge, verifier -> kronor, rank,
cap, coverage -> report. `CalcPost` is the typed calc the lenses read through `calc_posts` and kronor compares against.
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from anbud.models import Flag


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolCall(_Model):
    """One tool use inside a model loop. Coverage is computed from these alone."""

    role: str
    lens_id: str | None = None
    turn: int
    tool: str
    input: dict[str, Any]
    refused: str | None = Field(default=None, description="Why the loop refused the call, if it did")
    error: str | None = Field(default=None, description="Why the tool failed, if it did")
    result_ids: list[str] = Field(default_factory=list, description="Block, calc post or tile ids the call returned")


class LensResult(_Model):
    lens_id: str
    model: str
    status: Literal["submitted", "truncated", "no_submit", "error"]
    flags: list[Flag] = Field(default_factory=list)
    rejected: list[dict[str, Any]] = Field(
        default_factory=list, description="Flags that failed validation after the repair turn, with the reason"
    )
    tool_log: list[ToolCall] = Field(default_factory=list)
    turns: int = 0
    cost_usd: float = 0.0
    generic: bool = Field(default=False, description="Lens 8: normal scope the documents never mention")


class CalcPost(_Model):
    """One line of the calculation, typed. Amounts are the calc's own; no catalog price ever goes into a prompt."""

    id: str = Field(description="Stable id: calc file hash prefix plus sheet and row")
    sheet: str | None = None
    row: int | None = None
    section: str | None = Field(default=None, description="The heading or group the post sits under")
    text: str
    quantity: float | None = None
    unit: str | None = None
    unit_price: float | None = None
    amount: float | None = None
    hours: float | None = None
    hidden: bool = False


class RunInputs(_Model):
    """What a run reads: the prepared tender and the calc, both from the company's data folder."""

    company_id: str
    tender_id: str
    prep_dir: str
    calc_posts: list[CalcPost] = Field(default_factory=list)


def fingerprint(flag: Flag) -> str:
    """sha256 of the flag's sorted, de-duplicated block ids plus its claim type: the key labels and merges use."""
    ids = sorted({c.block_id for c in flag.citations})
    return hashlib.sha256(("\n".join(ids) + "\n" + flag.claim_type).encode()).hexdigest()
