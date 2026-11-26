"""The one tool list every tool-using role sends, and which role may call which tool.

- All tools are read-only and run in-process. No bash, no file system, no web, no URL the model chooses.
- The read tools are strict: the API guarantees their input matches the schema. The submit tools are not strict
  (their schemas are too large for strict mode's limits); code validates them with Pydantic and gives the model one
  repair turn.
- Every role in `ROLES` with tools sends the byte-identical list, so the cached prefix is shared. A role is limited by
  `refusal()` in the loop, never by sending a smaller list: a smaller list would rewrite the prefix on every call.
- The one exception: a role that declares its own list (`own_tools`) sends that list and nothing else. Only Kalkyl's
  Sonnet roles do; they never share a prefix with Granskning. Every Opus role sends the shared list (tested).
- Order matters for the cache: tools are sent in the order of `_TOOLS`. Changing a description, a field or the order is
  a prompt change, and `schemas/tools.json` shows it in the diff.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import cache
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from anbud.kalkyl.records import SubmitMatchesInput, SubmitQuantitiesInput
from anbud.models import Flag, TenderMap, Verdict
from anbud.schema import to_plain, to_strict


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SearchBlocksInput(_Input):
    query: str = Field(min_length=1, max_length=200, description="Words to look for. Ranked by BM25.")
    roles: list[str] | None = Field(
        default=None,
        max_length=10,
        description="Only search documents with these roles from the tender map, for example AF or drawing.",
    )


class ReadBlocksInput(_Input):
    ids: list[str] = Field(min_length=1, max_length=20, description="Block ids from search results or the outline.")


class OutlineInput(_Input):
    doc_id: str | None = Field(default=None, max_length=200, description="One document. Leave out for all documents.")


class CalcPostsInput(_Input):
    filter: str | None = Field(
        default=None,
        max_length=200,
        description="Words matched against post ids and texts. Leave out for every post.",
    )


class LawLookupInput(_Input):
    source: str = Field(min_length=1, max_length=100, description="The statute or regulation, as cited.")
    section: str = Field(min_length=1, max_length=100, description="The chapter and section, as cited.")


class ViewTileInput(_Input):
    sheet: str = Field(min_length=1, max_length=200, description="The drawing sheet id from the outline.")
    tile: str = Field(min_length=1, max_length=50, description="The tile id from the sheet's page card.")


class SubmitFlagsInput(_Input):
    flags: list[Flag] = Field(description="Every finding, each with citations and a money basis. Empty if none.")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    model: type[BaseModel]
    strict: bool


_TOOLS = (
    ToolSpec(
        "search_blocks",
        "Search the tender's blocks, outline and page cards. Returns block ids with short snippets.",
        SearchBlocksInput,
        strict=True,
    ),
    ToolSpec(
        "read_blocks",
        "Read whole blocks by id, with their document, page or row, and position.",
        ReadBlocksInput,
        strict=True,
    ),
    ToolSpec("outline", "The headings tree of one document or of all documents.", OutlineInput, strict=True),
    ToolSpec("calc_posts", "The calculation's posts, typed, with ids for citing.", CalcPostsInput, strict=True),
    ToolSpec(
        "law_lookup",
        "One section of a public statute or regulation from the harness's public-law corpus, verbatim.",
        LawLookupInput,
        strict=True,
    ),
    ToolSpec("view_tile", "One tile of a drawing sheet, as an image.", ViewTileInput, strict=True),
    ToolSpec(
        "submit_map",
        "Submit the tender map. Call it once, at the end. Code checks it; if a check fails you get the reasons and one "
        "chance to fix them.",
        TenderMap,
        strict=False,
    ),
    ToolSpec(
        "submit_flags",
        "Submit this lens's findings. Call it once, at the end. Code checks every citation and quote; if a check fails "
        "you get the reasons and one chance to fix them.",
        SubmitFlagsInput,
        strict=False,
    ),
    ToolSpec(
        "submit_verdict",
        "Submit the verdict on the flag under review. Call it once, at the end. Code checks it; if a check fails you "
        "get the reasons and one chance to fix them.",
        Verdict,
        strict=False,
    ),
)
TOOL_NAMES = tuple(t.name for t in _TOOLS)
INPUT_MODELS = {t.name: t.model for t in _TOOLS}

_READ = frozenset({"search_blocks", "read_blocks", "outline", "calc_posts"})


# Kalkyl's own tools: not part of the shared list, sent only by the roles that declare them.
_KALKYL_EXTRACT = (
    ToolSpec(
        "submit_quantities",
        "Submit this chunk's quantity records and set-aside lines. Call it once, at the end. Code checks every "
        "citation and quote; records that fail come back with the reason, once.",
        SubmitQuantitiesInput,
        strict=False,
    ),
)
_KALKYL_MATCH = (
    ToolSpec(
        "submit_matches",
        "Submit one catalog id per post, or none when no item fits. Call it once.",
        SubmitMatchesInput,
        strict=False,
    ),
)


@dataclass(frozen=True)
class Role:
    tools: frozenset[str]  # the tools the loop lets this role call; empty means the role is sent no tools
    shared_prefix: bool  # sends byte-identical system blocks: per model for the shared list, per role for own_tools
    own_tools: tuple[ToolSpec, ...] = ()  # sent instead of the shared list; `tools` must name exactly these


ROLES: dict[str, Role] = {
    "map": Role(_READ | {"submit_map"}, shared_prefix=True),
    "lens": Role(_READ | {"law_lookup", "view_tile", "submit_flags"}, shared_prefix=True),
    "lens_generic_scope": Role(_READ | {"submit_flags"}, shared_prefix=True),  # Sonnet writes its own prefix
    "verifier": Role(frozenset({"read_blocks", "search_blocks", "calc_posts", "submit_verdict"}), shared_prefix=True),
    "merge": Role(frozenset({"submit_flags"}), shared_prefix=False),
    "page_cards": Role(frozenset(), shared_prefix=False),  # reads tiles from its prompt; output format comes with prep
    "kalkyl_extract": Role(frozenset({"submit_quantities"}), shared_prefix=True, own_tools=_KALKYL_EXTRACT),
    "kalkyl_match": Role(frozenset({"submit_matches"}), shared_prefix=True, own_tools=_KALKYL_MATCH),
}


def _tool_json(specs: tuple[ToolSpec, ...]) -> str:
    tools = []
    for spec in specs:
        schema = spec.model.model_json_schema()
        tool: dict[str, Any] = {
            "name": spec.name,
            "description": spec.description,
            "input_schema": to_strict(schema) if spec.strict else to_plain(schema),
        }
        if spec.strict:
            tool["strict"] = True
        tools.append(tool)
    return json.dumps(tools, ensure_ascii=False)


@cache
def _shared_tools_json() -> str:
    return _tool_json(_TOOLS)


@cache
def _own_tools_json(role: str) -> str:
    return _tool_json(ROLES[role].own_tools)


def own_tools(role: str) -> list[dict[str, Any]]:
    """A fresh copy of the role's own list, for roles that declare one."""
    return json.loads(_own_tools_json(role))


def shared_tools() -> list[dict[str, Any]]:
    """A fresh copy of the shared list, identical in content on every call."""
    return json.loads(_shared_tools_json())


def tools_hash() -> str:
    """sha256 of the shared list as sent, for run logs and drift checks."""
    return hashlib.sha256(_shared_tools_json().encode()).hexdigest()


def snapshot_text() -> str:
    """The shared list as committed in schemas/tools.json."""
    return json.dumps(shared_tools(), ensure_ascii=False, indent=2) + "\n"


def refusal(role: str, tool: str) -> str | None:
    """The error text for the tool_result when `role` may not call `tool`, or None if it may. A refused call still
    counts as a turn."""
    own = {spec.name for spec in ROLES[role].own_tools}
    if tool not in INPUT_MODELS and tool not in own:
        return f"unknown tool: {tool}"
    if tool not in ROLES[role].tools:
        return f"{tool} is not available to this role"
    return None
