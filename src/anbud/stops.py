"""Per-run hard stops by tender size, from config/budgets.toml `[[granskning_stops]]` and `[[kalkyl_stops]]`.

- Granskning: the shared prefix in tokens. The run opens at the tier of the prefix its dry run estimates, before page
  cards add their text; that tier is the most it may spend. Once the map's first call has measured the prefix, the
  measured size picks the tier the rest of the run spends under, never above the one it opened with
  (`model_client.Budget.set_stop`). The estimate counts every selected document, search-only ones too, so it errs high.
- Kalkyl: its own $0 dry-run estimate (the high end) times its margin, before any paid call.

So a tender's dry runs give the most its runs can spend, and the exam's worst case holds. A size at a tier's bound
picks that tier, the lower one. A size above the top tier gets the top tier; a run that reaches its stop halts as
before, unfinished and resumable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

GRANSKNING = "granskning_stops"
KALKYL = "kalkyl_stops"
MEASURED = "measured"
ESTIMATE = "estimate"


@dataclass(frozen=True)
class Tier:
    name: str
    bound: float  # the largest size the tier takes: prefix tokens, or Kalkyl's estimate times its margin in USD
    usd: float


@dataclass(frozen=True)
class Stop:
    tier: str
    usd: float  # the tier's stop
    size: float  # what the tier was picked from
    source: str  # MEASURED or ESTIMATE
    above_top: bool = False
    in_force: float | None = None  # the stop the run spends under when the run's ceiling holds it below the tier's

    @property
    def hard_stop(self) -> float:
        return self.usd if self.in_force is None else self.in_force

    def held_at(self, usd: float) -> Stop:
        """This stop as the budget holds it: below the tier's when the run opened at a lower ceiling."""
        return self if usd >= self.usd else replace(self, in_force=usd)

    def summary(self) -> dict[str, Any]:
        """Counts only: safe to print and to log."""
        return {
            "tier": self.tier,
            "tier_usd": self.usd,
            "hard_stop_usd": self.hard_stop,
            "size": self.size,
            "from": self.source,
            "above_top_tier": self.above_top,
        }


def granskning_tiers(budgets: dict[str, Any]) -> tuple[Tier, ...]:
    raw = budgets[GRANSKNING]
    return _sorted(Tier(str(t["tier"]), float(t["max_prefix_tokens"]), float(t["hard_stop"])) for t in raw)


def kalkyl_tiers(budgets: dict[str, Any]) -> tuple[Tier, ...]:
    raw = budgets[KALKYL]
    return _sorted(Tier(str(t["tier"]), float(t["hard_stop"]), float(t["hard_stop"])) for t in raw)


def _sorted(tiers: Any) -> tuple[Tier, ...]:
    out = tuple(sorted(tiers, key=lambda t: t.bound))
    if not out:
        raise ValueError("budgets.toml: a stop table has no tiers")
    return out


def pick(tiers: Sequence[Tier], size: float, source: str) -> Stop:
    """The first tier whose bound takes `size`; the top tier when none does."""
    for tier in tiers:
        if size <= tier.bound:
            return Stop(tier.name, tier.usd, size, source)
    top = tiers[-1]
    return Stop(top.name, top.usd, size, source, above_top=True)


def granskning_stop(budgets: dict[str, Any], prefix_tokens: int, source: str) -> Stop:
    return pick(granskning_tiers(budgets), prefix_tokens, source)


def kalkyl_stop(tiers: Sequence[Tier], estimate_high_usd: float, margin: float) -> Stop:
    return pick(tiers, round(estimate_high_usd * margin, 4), ESTIMATE)


def top(tiers: Sequence[Tier]) -> float:
    return tiers[-1].usd


def prefix_label(stop: Stop) -> str:
    """For Granskning's report, in Swedish: 'M, 30 USD (prefix 212345 token, mätt)'."""
    how = "mätt" if stop.source == MEASURED else "uppskattat"
    held = f"; högst {stop.in_force:g} USD, nivån körningen startade med" if stop.in_force is not None else ""
    return f"{stop.tier}, {stop.usd:g} USD (prefix {stop.size:.0f} token, {how}{_over(stop)}){held}"


def estimate_label(stop: Stop) -> str:
    """For Kalkyl's overview, in Swedish: 'M, 8 USD (uppskattning med marginal 6.30 USD)'."""
    return f"{stop.tier}, {stop.usd:g} USD (uppskattning med marginal {stop.size:.2f} USD{_over(stop)})"


def _over(stop: Stop) -> str:
    return ", över översta nivån" if stop.above_top else ""
