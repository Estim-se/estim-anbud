"""The harness config in config/*.toml: model ids per role, token prices and budgets."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: float
    output: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float

    def cost(self, usage: Any) -> float:
        """What a response cost, from its `usage`. Cache writes without a 5m/1h split are charged at the 1h rate."""
        cache_creation = getattr(usage, "cache_creation", None)
        written = getattr(usage, "cache_creation_input_tokens", None) or 0
        write_5m = (cache_creation.ephemeral_5m_input_tokens or 0) if cache_creation else 0
        write_1h = (cache_creation.ephemeral_1h_input_tokens or 0) if cache_creation else 0
        write_1h += max(0, written - write_5m - write_1h)
        total = (
            usage.input_tokens * self.input
            + usage.output_tokens * self.output
            + write_5m * self.cache_write_5m
            + write_1h * self.cache_write_1h
            + (getattr(usage, "cache_read_input_tokens", None) or 0) * self.cache_read
        )
        return total / 1_000_000

    def max_cost(self, input_tokens: int, max_tokens: int) -> float:
        """The most a call can cost: every input token at the dearest input rate (a 1h cache write), every output token
        used, plus 5% because a token count is an estimate."""
        dearest_input = max(self.input, self.cache_write_5m, self.cache_write_1h)
        return 1.05 * (input_tokens * dearest_input + max_tokens * self.output) / 1_000_000


@dataclass(frozen=True)
class Config:
    api_base: str
    workspace: str  # also the Keychain account that holds this workspace's key
    models: dict[str, str]  # alias -> model id
    roles: dict[str, str]  # role -> alias
    prices: dict[str, Price]  # model id -> price
    budgets: dict[str, Any]

    def model_for(self, role: str) -> str:
        if role not in self.roles:
            raise KeyError(f"unknown role: {role}")
        return self.models[self.roles[role]]

    def price_for(self, model: str) -> Price:
        return self.prices[model]


def load_config(directory: Path = CONFIG_DIR) -> Config:
    providers = tomllib.loads((directory / "providers.toml").read_text())
    prices = tomllib.loads((directory / "prices.toml").read_text())
    budgets = tomllib.loads((directory / "budgets.toml").read_text())
    models = dict(providers["models"])
    return Config(
        api_base=providers["anthropic"]["api_base"],
        workspace=providers["anthropic"]["workspace"],
        models=models,
        roles=dict(providers["roles"]),
        prices={model: Price(**prices[model]) for model in models.values()},
        budgets=budgets,
    )
