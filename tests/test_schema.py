"""The strict-schema exporter and the complexity counter."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, Field

from anbud.schema import (
    MAX_OPTIONAL_PARAMS,
    MAX_STRICT_TOOLS,
    MAX_UNION_PARAMS,
    SchemaError,
    complexity,
    to_plain,
    to_strict,
)


class _Inner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    note: str | None = None


class _Sample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=50, description="A name")
    when: str = Field(json_schema_extra={"format": "date"})
    tags: list[str] = Field(min_length=2, max_length=5)
    ids: list[str] = Field(min_length=1)
    count: int = Field(ge=0, le=10)
    inner: _Inner
    label: str | None = Field(default=None, description="Optional label")


def test_strict_export_drops_what_strict_mode_cant_express() -> None:
    schema = to_strict(_Sample.model_json_schema())
    props = schema["properties"]
    assert props["name"] == {"description": "A name", "type": "string"}
    assert props["when"]["format"] == "date"
    assert "minItems" not in props["tags"] and "maxItems" not in props["tags"]  # only 0 and 1 are allowed
    assert props["ids"]["minItems"] == 1
    assert props["count"] == {"type": "integer"}
    assert props["label"] == {"type": "string", "description": "Optional label"}  # not a union with null
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["_Inner"]["additionalProperties"] is False
    assert schema["$defs"]["_Inner"]["properties"]["note"] == {"type": "string"}
    assert "title" not in str(schema)


def test_strict_export_drops_unsupported_formats() -> None:
    schema = to_strict({"type": "object", "properties": {"x": {"type": "string", "format": "phone"}}, "required": []})
    assert schema["properties"]["x"] == {"type": "string"}


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "object", "properties": {"x": {"oneOf": [{"type": "string"}, {"type": "integer"}]}}},
        {"type": "object", "properties": {"x": {"not": {"type": "string"}}}},
        {"type": "object", "properties": {"x": {"type": "string", "anyOf": [{"type": "string"}]}}},
        {"type": "object", "properties": {}, "additionalProperties": True},
        {"type": "object", "properties": {"x": {"type": "object", "additionalProperties": {"type": "string"}}}},
        {"type": "object", "properties": {"x": {"enum": [{"a": 1}]}}},
        {"type": "array", "items": {"type": "string"}},
    ],
)
def test_strict_export_refuses_what_would_change_meaning(bad: dict) -> None:
    with pytest.raises(SchemaError):
        to_strict(bad)


def test_plain_export_strips_titles_but_keeps_a_field_named_title() -> None:
    class Doc(BaseModel):
        title: str

    schema = to_plain(Doc.model_json_schema())
    assert "title" in schema["properties"]
    assert "title" not in schema and "title" not in schema["properties"]["title"]


def _tool(name: str, schema: dict, strict: bool = True) -> dict:
    tool = {"name": name, "description": "x", "input_schema": schema}
    if strict:
        tool["strict"] = True
    return tool


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


def test_counter_counts_optional_and_union_parameters_at_every_depth() -> None:
    nested = _obj({"a": {"type": "string"}, "b": {"type": ["string", "null"]}}, ["a"])
    schema = _obj({"x": {"type": "string"}, "y": nested, "z": {"anyOf": [{"type": "string"}, {"type": "integer"}]}})
    report = complexity([_tool("t", schema), _tool("loose", _obj({"q": {"type": "string"}}), strict=False)])
    assert (report.strict_tools, report.optional_params, report.union_params) == (1, 4, 2)
    assert report.ok


def test_counter_expands_refs_per_use_and_catches_recursion() -> None:
    shared = _obj({"o": {"type": "string"}})
    schema = {**_obj({"a": {"$ref": "#/$defs/S"}, "b": {"$ref": "#/$defs/S"}}, ["a", "b"]), "$defs": {"S": shared}}
    assert complexity([_tool("t", schema)]).optional_params == 2
    looping = {**_obj({"next": {"$ref": "#/$defs/N"}}, ["next"]), "$defs": {"N": _obj({"n": {"$ref": "#/$defs/N"}})}}
    assert any("recursive" in p for p in complexity([_tool("t", looping)]).problems)


def test_counter_flags_leftover_keywords_and_open_objects() -> None:
    open_object = {"type": "object", "properties": {"x": {"type": "string", "minLength": 2}}, "required": ["x"]}
    problems = complexity([_tool("t", open_object)]).problems
    assert any("minLength" in p for p in problems)
    assert any("additionalProperties" in p for p in problems)


def test_counter_enforces_each_limit() -> None:
    one = _obj({"x": {"type": "string"}}, ["x"])
    assert not complexity([_tool(f"t{i}", one) for i in range(MAX_STRICT_TOOLS + 1)]).ok
    many_optional = _obj({f"p{i}": {"type": "string"} for i in range(MAX_OPTIONAL_PARAMS + 1)})
    assert not complexity([_tool("t", many_optional)]).ok
    union = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
    many_unions = _obj({f"u{i}": union for i in range(MAX_UNION_PARAMS + 1)}, [f"u{i}" for i in range(17)])
    assert not complexity([_tool("t", many_unions)]).ok
