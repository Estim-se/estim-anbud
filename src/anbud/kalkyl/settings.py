"""Kalkyl's settings: chunk sizes, effort, `max_tokens` and turn caps from config/kalkyl.toml, the run's hard stop from
budgets.toml [kalkyl]. Models come from providers.toml [roles] like every role's.

Roles (in `anbud.tools.ROLES`, each with its own tool list):
- `kalkyl_extract` (R2): reads one chunk and calls `submit_quantities`.
- `kalkyl_match` (R3): picks a catalog id, or none, for posts R2 left unmapped, through `submit_matches`.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from anbud.config import CONFIG_DIR
from anbud.model_client import EFFORTS

EXTRACT = "kalkyl_extract"
MATCH = "kalkyl_match"
NO_MATCH = "none"


@dataclass(frozen=True)
class RoleSettings:
    effort: str
    max_tokens: int
    max_turns: int = 1


@dataclass(frozen=True)
class KalkylConfig:
    hard_stop: float
    default_catalog: str
    chunk_max_chars: int
    chunk_max_quantity_lines: int
    concurrency: int
    match_batch: int
    roles: dict[str, RoleSettings]


def load_kalkyl_config(directory: Path = CONFIG_DIR) -> KalkylConfig:
    raw = tomllib.loads((directory / "kalkyl.toml").read_text())
    budgets = tomllib.loads((directory / "budgets.toml").read_text())
    ex, ma = raw["extract"], raw["match"]
    roles = {
        EXTRACT: RoleSettings(ex["effort"], int(ex["max_tokens"]), int(ex["max_turns"])),
        MATCH: RoleSettings(ma["effort"], int(ma["max_tokens"]), 1),
    }
    for settings in roles.values():
        if settings.effort not in EFFORTS:
            raise ValueError(f"kalkyl.toml: effort must be one of {EFFORTS}")
    return KalkylConfig(
        hard_stop=float(budgets["kalkyl"]["hard_stop"]),
        default_catalog=raw["catalog"]["default"],
        chunk_max_chars=int(raw["chunks"]["max_chars"]),
        chunk_max_quantity_lines=int(raw["chunks"]["max_quantity_lines"]),
        concurrency=max(1, int(ex["concurrency"])),
        match_batch=max(1, int(ma["batch_size"])),
        roles=roles,
    )
