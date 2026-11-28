"""One finding as it moves through merge, verify, kronor and rank. Built by merge, then copied with more fields set."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from anbud.models import Flag, MoneyBasis, Verdict


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Kronor(_Model):
    """What code priced a money basis at. `default_catalog` marks figures from Estim's default catalog."""

    priced: bool
    point: float | None = None
    low: float | None = None
    high: float | None = None
    basis: MoneyBasis = Field(description="The money basis that was priced, or that couldn't be")
    label: str = Field(default="", description="How the figure was computed, in Swedish, with no catalog price in it")
    catalog: str | None = None
    default_catalog: bool = False
    reason: str | None = Field(default=None, description="Why it couldn't be priced")


Verification = Literal["verified", "sample", "failed", "budget", "not_selected"]  # budget: the hard stop came first
Placement = Literal["shown", "shown_unpriced", "generic", "minor", "refuted", "covered", "capped", "zero"]


class Item(_Model):
    flag: Flag
    fingerprint: str
    lenses: list[str]
    generic: bool = False
    sources: list[str] = Field(default_factory=list, description="Fingerprints of the lens flags merged into this one")
    source_flags: list[Flag] = Field(
        default_factory=list, description="The lens flags merged into this one, when there were several"
    )
    kronor: Kronor | None = None
    verdict: Verdict | None = None
    verification: Verification = "not_selected"
    score: float = 0.0
    minor: bool = False
    placement: Placement | None = None
