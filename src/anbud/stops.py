"""Per-run hard stops by tender size, from config/budgets.toml `[[granskning_stops]]` and `[[kalkyl_stops]]`.

- Granskning: the shared prefix in tokens. The run opens at the tier of the prefix its dry run estimates, before page
  cards add their text; that tier is the most it may spend. Once the map's first call has measured the prefix, the
  measured size picks the tier the rest of the run spends under, never above the one it opened with
  (`model_client.Budget.set_stop`). The estimate counts every selected document, search-only ones too, so it errs high.
- Kalkyl: its own $0 dry-run estimate (the high end) times its margin, before any paid call.

So a tender's dry runs give the most its runs can spend, and the exam's worst case holds. A size at a tier's bound
picks that tier, the lower one. A size above the top tier gets the top tier; a run that reaches its stop halts as
before, unfinished and resumable.

Scaled stops (budgets.toml `[granskning]`, `[kalkyl]`, `[baseline]`): each run's stop is raised to a floor scaled to
its size, so a cap never cuts off normal work, and is never below its tier's stop. Each floor is fixed before the first
paid call, from the same numbers the dry run prints, and the exam records the same stop as the run's cap
(`granskning_run_stop`, `kalkyl_run_stop`, `baseline_run_stop`). An engine's optional `max_scaled_stop` caps its floor.
- Granskning: its page cards and the map's reservation take at most `card_map_share` of the stop, so the lenses keep
  the rest. A run that lowers its tier on the measured prefix never goes below the floor (`Budget.set_stop`).
- Kalkyl: its dry run's high estimate times `spend_ratio`, its measured spend over that estimate plus a size margin.
- The old engine: its pages times `usd_per_page` times `margin`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

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
    floor: float = 0.0  # scaled to the run's size (`scaled`); the run's stop is never below it

    @property
    def opening(self) -> float:
        """The stop before any ceiling holds it: the tier's, or the floor when that is higher."""
        return max(self.usd, self.floor)

    @property
    def hard_stop(self) -> float:
        return self.opening if self.in_force is None else self.in_force

    def held_at(self, usd: float) -> Stop:
        """This stop as the budget holds it: below the tier's when the run opened at a lower ceiling."""
        return self if usd >= self.opening else replace(self, in_force=usd)

    def summary(self) -> dict[str, Any]:
        """Counts only: safe to print and to log."""
        return {
            "tier": self.tier,
            "tier_usd": self.usd,
            "hard_stop_usd": self.hard_stop,
            "size": self.size,
            "from": self.source,
            "above_top_tier": self.above_top,
            "floor_usd": self.floor,
            "scaled": self.floor > self.usd,
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


def scaled(stop: Stop, floor_usd: float, ceiling_usd: float | None = None) -> Stop:
    """`stop` with its floor: `floor_usd`, held at `ceiling_usd` (an engine's `max_scaled_stop`) when one is set. The
    stop in force is the higher of the tier's and the floor, so never below the tier's."""
    floor = floor_usd if ceiling_usd is None else min(floor_usd, ceiling_usd)
    return replace(stop, floor=round(max(floor, 0.0), 4))


def max_scaled_stop(section: dict[str, Any]) -> float | None:
    """An engine's optional ceiling on its scaled stop; unset by default."""
    value = section.get("max_scaled_stop")
    return None if value is None else float(value)


class _Config(Protocol):
    budgets: dict[str, Any]

    def model_for(self, role: str) -> str: ...

    def price_for(self, model: str) -> Any: ...


def cards_and_map_usd(config: _Config, prefix_tokens: int, tiles: int) -> float:
    """What a Granskning run may spend before its lenses: every page card (`card_usd_per_tile` each), then the map's
    reservation, the prefix at the map model's dearest input price plus its max tokens. Neither degrades, so a refusal
    of either halts the run (audit 2, C10)."""
    from anbud.granskning.mapping import MAP_MAX_TOKENS

    price = config.price_for(config.model_for("map"))
    cards = tiles * float(config.budgets["granskning"]["card_usd_per_tile"])
    return cards + price.max_cost(int(prefix_tokens), MAP_MAX_TOKENS)


def granskning_run_stop(config: _Config, prefix_tokens: int, tiles: int) -> Stop:
    """The stop a Granskning run opens at, from its dry run's estimated prefix and the tiles its page cards would read
    (both as the dry run prints them): the prefix's tier, raised so its cards and map take at most `card_map_share`."""
    section = config.budgets["granskning"]
    floor = cards_and_map_usd(config, prefix_tokens, tiles) / float(section["card_map_share"])
    return scaled(granskning_stop(config.budgets, prefix_tokens, ESTIMATE), floor, max_scaled_stop(section))


class _Kalkyl(Protocol):
    stops: tuple[Tier, ...]
    margin: float
    spend_ratio: float
    max_scaled_stop: float | None


def kalkyl_run_stop(kalkyl: _Kalkyl, high_usd: float) -> Stop:
    """The stop a Kalkyl run spends under, from its dry run's `cost_usd_est_high` (`kalkyl.settings.KalkylConfig`):
    the tier its high estimate times the margin picks, raised to the high estimate times `spend_ratio`."""
    stop = kalkyl_stop(kalkyl.stops, high_usd, kalkyl.margin)
    return scaled(stop, high_usd * kalkyl.spend_ratio, kalkyl.max_scaled_stop)


def baseline_run_stop(budgets: dict[str, Any], pages: int) -> float:
    """The old engine's stop for a run on `pages` pages (its prep's `pages_by_kind`, summed): the configured stop,
    raised to the pages times `usd_per_page` times `margin`."""
    section = budgets["baseline"]
    floor = pages * float(section["usd_per_page"]) * float(section["margin"])
    ceiling = max_scaled_stop(section)
    if ceiling is not None:
        floor = min(floor, ceiling)
    return round(max(float(section["hard_stop"]), floor), 4)


def top(tiers: Sequence[Tier]) -> float:
    return tiers[-1].usd


def prefix_label(stop: Stop) -> str:
    """For Granskning's report, in Swedish: 'M, 30 USD (prefix 212345 token, mätt)'."""
    how = "mätt" if stop.source == MEASURED else "uppskattat"
    raised = f"; höjt till {stop.floor:.2f} USD för sidkort och karta" if stop.floor > stop.usd else ""
    held = f"; högst {stop.in_force:g} USD, nivån körningen startade med" if stop.in_force is not None else ""
    return f"{stop.tier}, {stop.usd:g} USD (prefix {stop.size:.0f} token, {how}{_over(stop)}){raised}{held}"


def estimate_label(stop: Stop) -> str:
    """For Kalkyl's overview, in Swedish: 'M, 8 USD (uppskattning med marginal 6.30 USD)'."""
    raised = f"; höjt till {stop.floor:.2f} USD efter uppskattningen" if stop.floor > stop.usd else ""
    return f"{stop.tier}, {stop.usd:g} USD (uppskattning med marginal {stop.size:.2f} USD{_over(stop)}){raised}"


def _over(stop: Stop) -> str:
    return ", över översta nivån" if stop.above_top else ""
