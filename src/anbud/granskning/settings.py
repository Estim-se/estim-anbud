"""Granskning's settings after the lenses, from config/granskning.toml and budgets.toml [verifier]."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from anbud.config import CONFIG_DIR


@dataclass(frozen=True)
class Settings:
    default_catalog: str = "estim-default"
    materiality_sek: float = 20000.0
    unpriced_sek: float = 20000.0
    guarantee_cost_percent_per_year: float = 1.0
    spread: dict[str, float] = field(default_factory=dict)
    verdict_weights: dict[str, float] = field(default_factory=dict)
    covered_by_calc: float = 0.0
    other_trade: float = 0.5
    max_shown: int = 15
    max_generic: int = 10
    default_action_rate: float = 1.0
    verifier_top_n: int = 25
    verifier_sampled: int = 3
    verifier_max_turns: int = 3
    verifier_max_usd: float = 0.40
    verifier_max_tokens: int = 12000
    verifier_effort: str = "high"
    merge_max_tokens: int = 8000
    merge_effort: str = "medium"

    def weight(self, verdict: str | None) -> float:
        return self.verdict_weights.get(verdict or "unverified", self.verdict_weights.get("unverified", 0.0))


def load_settings(directory: Path = CONFIG_DIR) -> Settings:
    g = tomllib.loads((directory / "granskning.toml").read_text())
    verifier = tomllib.loads((directory / "budgets.toml").read_text()).get("verifier", {})
    k = g["kronor"]
    return Settings(
        default_catalog=k["default_catalog"],
        materiality_sek=float(k["materiality_sek"]),
        unpriced_sek=float(k["unpriced_sek"]),
        guarantee_cost_percent_per_year=float(k["guarantee_cost_percent_per_year"]),
        spread={name: float(v) for name, v in k["spread"].items()},
        verdict_weights={name: float(v) for name, v in g["verdict_weights"].items()},
        covered_by_calc=float(g["adjustments"]["covered_by_calc"]),
        other_trade=float(g["adjustments"]["other_trade"]),
        max_shown=int(g["rank"]["max_shown"]),
        max_generic=int(g["rank"]["max_generic"]),
        default_action_rate=float(g["rank"]["default_action_rate"]),
        verifier_top_n=int(verifier.get("top_n", 25)),
        verifier_sampled=int(verifier.get("sampled_below_cut", 3)),
        verifier_max_turns=int(verifier.get("max_turns", 3)),
        verifier_max_usd=float(g["verifier"]["max_usd"]),
        verifier_max_tokens=int(g["verifier"]["max_tokens"]),
        verifier_effort=str(g["verifier"]["effort"]),
        merge_max_tokens=int(g["merge"]["max_tokens"]),
        merge_effort=str(g["merge"]["effort"]),
    )
