"""Tool schemas the API accepts, and a count of what strict mode allows per request.

Strict tools are compiled into a grammar. The API allows, across all strict schemas in one request, at most 20 strict
tools, 24 optional parameters and 16 union-type parameters, and refuses some JSON Schema keywords
(https://platform.claude.com/docs/en/build-with-claude/structured-outputs, "Schema complexity limits" and "JSON
Schema limitations", checked 2026-11-26).

- `to_strict` turns a Pydantic model's schema into one strict mode accepts. Constraints strict mode can't express
  (lengths, bounds, item counts) are dropped from what is sent; the Pydantic model still enforces them in code.
  Keywords that would change the meaning if dropped (`oneOf`, `not`, conditionals) are refused.
- An optional parameter is sent as its plain type, not as a union with null: absent means None. That keeps the union
  count at zero for the read tools.
- `to_plain` is for non-strict tools: the Pydantic schema without titles.
- `complexity` counts a tool list against the limits. CI runs it on the shared list, so a change that would make every
  call return 400 fails before any money is spent.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

MAX_STRICT_TOOLS = 20
MAX_OPTIONAL_PARAMS = 24
MAX_UNION_PARAMS = 16

# Dropped from strict schemas and enforced in code by the Pydantic model instead.
_DROPPED = {
    "minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "maxItems", "uniqueItems", "minProperties", "maxProperties", "title", "examples",
}  # fmt: skip
# Refused: dropping them would change what the schema means.
_REFUSED = {
    "oneOf", "not", "if", "then", "else", "patternProperties", "propertyNames", "dependentSchemas",
    "dependentRequired", "prefixItems", "contains", "unevaluatedProperties", "unevaluatedItems",
}  # fmt: skip
_FORMATS = {"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"}
_ANNOTATIONS = {"description", "default"}
_DATA = {"default", "const", "enum", "examples"}  # values, not schemas: never rewritten
# Regex features strict mode can't compile: lookaround, backreferences, word boundaries.
_BAD_REGEX = re.compile(r"\(\?<?[=!]|\\[1-9]|\\[bB]")


class SchemaError(ValueError):
    pass


def _is_null(schema: Any) -> bool:
    return isinstance(schema, dict) and schema.get("type") == "null" and len(schema) == 1


def _strict_node(node: Any, where: str) -> Any:
    if not isinstance(node, dict):
        raise SchemaError(f"{where}: a schema must be an object")
    refused = _REFUSED & node.keys()
    if refused:
        raise SchemaError(f"{where}: strict mode doesn't support {sorted(refused)}")
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _DROPPED:
            continue
        if key == "format" and value not in _FORMATS:
            continue
        if key == "minItems" and value > 1:
            continue  # strict mode allows only 0 and 1
        if key == "pattern" and _BAD_REGEX.search(value):
            raise SchemaError(f"{where}: strict mode can't compile lookaround, backreferences or word boundaries")
        if key == "properties":
            out[key] = {name: _strict_node(sub, f"{where}.{name}") for name, sub in value.items()}
        elif key in ("items", "additionalItems"):
            out[key] = _strict_node(value, f"{where}[]")
        elif key in ("anyOf", "allOf"):
            out[key] = [_strict_node(sub, f"{where}|{i}") for i, sub in enumerate(value)]
        elif key in ("$defs", "definitions"):
            out[key] = {name: _strict_node(sub, f"#{name}") for name, sub in value.items()}
        elif key in ("enum", "const"):
            values = value if key == "enum" else [value]
            if any(not isinstance(v, str | int | float | bool | type(None)) for v in values):
                raise SchemaError(f"{where}: enum and const values must be strings, numbers, booleans or null")
            out[key] = value
        else:
            out[key] = value
    if "anyOf" in out and ({"type", "properties"} & out.keys()):
        raise SchemaError(f"{where}: anyOf can't sit next to type or properties")
    if out.get("type") == "object" or "properties" in out:
        if out.get("additionalProperties", False) is not False:
            raise SchemaError(f"{where}: strict objects can't allow additional properties")
        out["additionalProperties"] = False
        required = set(out.get("required", []))
        for name, sub in list(out.get("properties", {}).items()):
            if name not in required:
                out["properties"][name] = _drop_null_branch(sub)
    return out


def _drop_null_branch(prop: dict[str, Any]) -> dict[str, Any]:
    """An optional `X | None = None` becomes plain X: the parameter is left out instead of sent as null."""
    branches = prop.get("anyOf")
    if not branches or len(branches) != 2 or not any(map(_is_null, branches)):
        return prop
    if prop.get("default", None) is not None:
        return prop
    (kept,) = [b for b in branches if not _is_null(b)]
    merged = {k: v for k, v in prop.items() if k in _ANNOTATIONS - {"default"}}
    return {**kept, **merged}


def to_strict(schema: dict[str, Any]) -> dict[str, Any]:
    """A strict-mode input schema from a Pydantic `model_json_schema()`."""
    out = _strict_node(copy.deepcopy(schema), "$")
    out.pop("description", None)  # the tool's own description says what it does
    if out.get("type") != "object":
        raise SchemaError("$: a tool's input schema must be an object")
    return out


def _strip_titles(node: Any) -> Any:
    if isinstance(node, list):
        return [_strip_titles(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in node.items():
        if key == "title" and isinstance(value, str):
            continue
        if key in _DATA:
            out[key] = value
        elif key in ("properties", "$defs", "definitions"):
            out[key] = {name: _strip_titles(sub) for name, sub in value.items()}  # names are kept, even "title"
        else:
            out[key] = _strip_titles(value)
    return out


def to_plain(schema: dict[str, Any]) -> dict[str, Any]:
    """A non-strict input schema: the Pydantic schema without titles. Code validates the input with the model."""
    out = _strip_titles(copy.deepcopy(schema))
    out.pop("description", None)
    return out


@dataclass
class Complexity:
    strict_tools: int = 0
    optional_params: int = 0
    union_params: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            not self.problems
            and self.strict_tools <= MAX_STRICT_TOOLS
            and self.optional_params <= MAX_OPTIONAL_PARAMS
            and self.union_params <= MAX_UNION_PARAMS
        )

    def summary(self) -> str:
        return (
            f"strict tools {self.strict_tools}/{MAX_STRICT_TOOLS}, "
            f"optional parameters {self.optional_params}/{MAX_OPTIONAL_PARAMS}, "
            f"union parameters {self.union_params}/{MAX_UNION_PARAMS}"
        )


def _resolve(ref: str, root: dict[str, Any]) -> Any:
    """`#/$defs/X` looks only in `$defs`, `#/definitions/X` only in `definitions`."""
    for section in ("$defs", "definitions"):
        prefix = f"#/{section}/"
        if ref.startswith(prefix) and ref[len(prefix) :] in root.get(section, {}):
            return root[section][ref[len(prefix) :]]
    return None


def _count(node: Any, root: dict[str, Any], tool: str, path: tuple[str, ...], report: Complexity) -> None:
    """Refs are expanded where they're used, so a shared definition counts once per use (the conservative reading)."""
    if not isinstance(node, dict):
        report.problems.append(f"{tool}: a schema that isn't an object")
        return
    if "$ref" in node:
        ref = node["$ref"]
        target = _resolve(ref, root)
        if target is None:
            report.problems.append(f"{tool}: unresolvable $ref {ref}")
        elif ref in path:
            report.problems.append(f"{tool}: recursive schema via {ref}")
            return
        else:
            _count(target, root, tool, (*path, ref), report)
    if "pattern" in node and _BAD_REGEX.search(node["pattern"]):
        report.problems.append(f"{tool}: a pattern strict mode can't compile")
    leftover = (_DROPPED - {"title"} | _REFUSED) & node.keys()
    if leftover:
        report.problems.append(f"{tool}: unsupported keywords {sorted(leftover)}")
    if node.get("minItems", 0) > 1:
        report.problems.append(f"{tool}: minItems above 1")
    if "format" in node and node["format"] not in _FORMATS:
        report.problems.append(f"{tool}: unsupported format {node['format']}")
    if "anyOf" in node or isinstance(node.get("type"), list):
        report.union_params += 1
    if node.get("type") == "object" or "properties" in node:
        if node.get("additionalProperties") is not False:
            report.problems.append(f"{tool}: an object without additionalProperties false")
        properties = node.get("properties", {})
        report.optional_params += len(set(properties) - set(node.get("required", [])))
        for sub in properties.values():
            _count(sub, root, tool, path, report)
    for key in ("items", "additionalItems"):
        if isinstance(node.get(key), dict):
            _count(node[key], root, tool, path, report)
    for key in ("anyOf", "allOf"):
        for sub in node.get(key, []):
            _count(sub, root, tool, path, report)


def complexity(tools: list[dict[str, Any]]) -> Complexity:
    """Counts across every strict tool in one request, as the API does. Definitions nothing refers to are still checked
    for problems (the API compiles them too), but add nothing to the counts."""
    report = Complexity()
    for tool in tools:
        if not tool.get("strict"):
            continue
        report.strict_tools += 1
        schema = tool["input_schema"]
        _count(schema, schema, tool["name"], (), report)
        for section in ("$defs", "definitions"):
            for name, definition in schema.get(section, {}).items():
                scratch = Complexity()
                _count(definition, schema, tool["name"], (f"#/{section}/{name}",), scratch)
                report.problems += [p for p in scratch.problems if p not in report.problems]
    return report
