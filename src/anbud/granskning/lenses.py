"""Lens files: `lenses/<lens_id>/SKILL.md`, a small front matter and a body that becomes the lens's instructions.

Front matter, one `key: value` per line between `---` lines:
    name: the lens id (must equal the folder name)
    description: one line, shown to the map so it can plan the lens
    role: `lens` (Opus) or `lens_generic_scope` (Sonnet)
    tools: [search_blocks, read_blocks, ...]   the tools this lens may call, a subset of its role's tools
    generic: true for the lens whose flags are reported separately as normal scope
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from anbud.paths import repo_root
from anbud.tools import ROLES

LENS_DIR = repo_root() / "lenses"


class LensFileError(ValueError):
    pass


@dataclass(frozen=True)
class Lens:
    id: str
    description: str
    role: str
    tools: frozenset[str]
    generic: bool
    body: str


def _front_matter(text: str, where: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise LensFileError(f"{where}: no front matter")
    try:
        end = next(i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration:
        raise LensFileError(f"{where}: front matter isn't closed") from None
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise LensFileError(f"{where}: front matter line without a colon")
        fields[key.strip()] = value.strip()
    return fields, "\n".join(lines[end + 1 :]).strip() + "\n"


def parse_lens(path: Path) -> Lens:
    where = f"{path.parent.name}/{path.name}"
    fields, body = _front_matter(path.read_text(encoding="utf-8"), where)
    for key in ("name", "description", "role", "tools"):
        if not fields.get(key):
            raise LensFileError(f"{where}: missing {key}")
    if fields["name"] != path.parent.name:
        raise LensFileError(f"{where}: name doesn't match the folder")
    role = fields["role"]
    if role not in ("lens", "lens_generic_scope"):
        raise LensFileError(f"{where}: role must be lens or lens_generic_scope")
    tools = frozenset(t.strip() for t in fields["tools"].strip("[]").split(",") if t.strip())
    extra = tools - ROLES[role].tools
    if extra:
        raise LensFileError(f"{where}: tools its role may not call: {', '.join(sorted(extra))}")
    if "submit_flags" not in tools:
        raise LensFileError(f"{where}: a lens must be able to call submit_flags")
    return Lens(
        id=fields["name"],
        description=fields["description"],
        role=role,
        tools=tools,
        generic=fields.get("generic", "false").lower() == "true",
        body=body,
    )


def load_lenses(directory: Path = LENS_DIR) -> list[Lens]:
    """Every lens, in folder-name order."""
    lenses = [parse_lens(p) for p in sorted(directory.glob("*/SKILL.md"))]
    if len({lens.id for lens in lenses}) != len(lenses):
        raise LensFileError("two lenses share an id")
    return lenses
