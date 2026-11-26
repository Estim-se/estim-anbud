"""Kalkyl's model roles and tools, on top of ModelClient.

Kalkyl calls aren't Granskning roles: they don't send the shared Opus tool list, and their tools carry the run's
catalog ids. Until `anbud.tools.ROLES` and `config/providers.toml` gain Kalkyl roles, `KalkylClient` subclasses
ModelClient and overrides only how a request is built: streaming, the budget reservation, the stop marker, the step
cache, events and error mapping are ModelClient's own. Each role keeps a byte-identical tools and system prefix within
a run, checked like the shared prefix, so R2's cache is written once.

Roles:
- `kalkyl_extract` (R2): reads one chunk and calls `submit_quantities`.
- `kalkyl_match` (R3): picks a catalog id, or none, for posts R2 left unmapped, through `submit_matches`.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from anbud.config import CONFIG_DIR, Config
from anbud.kalkyl.records import SubmitQuantitiesInput
from anbud.model_client import EFFORTS, ModelClient, PrefixDrift, _digest
from anbud.schema import to_plain

EXTRACT = "kalkyl_extract"
MATCH = "kalkyl_match"
NO_MATCH = "none"


@dataclass(frozen=True)
class RoleSettings:
    model: str  # alias in providers.toml [models]
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
    ex, ma = raw["extract"], raw["match"]
    roles = {
        EXTRACT: RoleSettings(ex["model"], ex["effort"], int(ex["max_tokens"]), int(ex["max_turns"])),
        MATCH: RoleSettings(ma["model"], ma["effort"], int(ma["max_tokens"]), 1),
    }
    for settings in roles.values():
        if settings.effort not in EFFORTS:
            raise ValueError(f"kalkyl.toml: effort must be one of {EFFORTS}")
    return KalkylConfig(
        hard_stop=float(raw["run"]["hard_stop"]),
        default_catalog=raw["catalog"]["default"],
        chunk_max_chars=int(raw["chunks"]["max_chars"]),
        chunk_max_quantity_lines=int(raw["chunks"]["max_quantity_lines"]),
        concurrency=max(1, int(ex["concurrency"])),
        match_batch=max(1, int(ma["batch_size"])),
        roles=roles,
    )


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Match(_Input):
    post_id: str = Field(min_length=1, max_length=40)
    catalog_id: str = Field(min_length=1, max_length=80, description="A catalog id from the list, or none")
    reason: str = Field(default="", max_length=300)


class SubmitMatchesInput(_Input):
    matches: list[Match] = Field(description="One entry per post in the request")


def _with_catalog_enum(schema: dict[str, Any], ids: list[str], *, allow_none: bool) -> dict[str, Any]:
    """Constrain every `catalog_id` property to the catalog's ids (and `none` when asked)."""
    choices = [*ids, NO_MATCH] if allow_none else list(ids)
    if not choices:
        return schema

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            props = node.get("properties", {})
            if "catalog_id" in props:
                desc = props["catalog_id"].get("description")
                props["catalog_id"] = {"type": "string", "enum": choices, **({"description": desc} if desc else {})}
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    return schema


def extract_tools(catalog_ids: list[str]) -> list[dict[str, Any]]:
    schema = _with_catalog_enum(to_plain(SubmitQuantitiesInput.model_json_schema()), catalog_ids, allow_none=False)
    return [
        {
            "name": "submit_quantities",
            "description": "Submit this chunk's quantity records and set-aside lines. Call it once, at the end. Code "
            "checks every citation and quote; records that fail come back with the reason, once.",
            "input_schema": schema,
        }
    ]


def match_tools(catalog_ids: list[str]) -> list[dict[str, Any]]:
    schema = _with_catalog_enum(to_plain(SubmitMatchesInput.model_json_schema()), catalog_ids, allow_none=True)
    return [
        {
            "name": "submit_matches",
            "description": "Submit one catalog id per post, or none when no item fits. Call it once.",
            "input_schema": schema,
        }
    ]


class KalkylClient(ModelClient):
    """ModelClient with Kalkyl's roles. Pass `role_tools` as built by `extract_tools` and `match_tools`."""

    def __init__(
        self,
        config: Config,
        sdk: Any,
        budget: Any,
        *,
        kalkyl: KalkylConfig,
        role_tools: dict[str, list[dict[str, Any]]],
        **kwargs: Any,
    ) -> None:
        super().__init__(config, sdk, budget, **kwargs)
        self.kalkyl = kalkyl
        self.role_tools = role_tools

    def request(
        self, *, role: str, system: list[dict[str, Any]], messages: list[dict[str, Any]], max_tokens: int, effort: str
    ) -> dict[str, Any]:
        settings = self.kalkyl.roles.get(role)
        if settings is None:
            raise KeyError(f"unknown Kalkyl role: {role}")
        if effort not in EFFORTS:
            raise ValueError(f"effort must be one of {EFFORTS}")
        if not 1 <= max_tokens <= settings.max_tokens:
            raise ValueError(f"max_tokens must be between 1 and the role's cap of {settings.max_tokens}")
        params: dict[str, Any] = {
            "model": self.config.models[settings.model],
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "output_config": {"effort": effort},
        }
        tools = self.role_tools.get(role)
        if tools:
            params["tools"] = tools
            params["tool_choice"] = {"type": "auto"}
        return params

    def _check_prefix(self, role: str, params: dict[str, Any]) -> None:
        digest = _digest(params.get("tools"), params["system"])
        with self._lock:
            first = self._prefixes.setdefault(role, digest)
        if first != digest:
            raise PrefixDrift(f"{role} would send a different tools or system prefix than its earlier calls")
