"""The read tools, in process: search_blocks, read_blocks, outline, calc_posts, law_lookup, view_tile and
catalog_search (ids, names, units and categories only: prompts never carry a catalog price).

Each call returns the `tool_result` content, the ids it returned (for coverage) and an error, if any. Text that comes
from the tender or the calc is wrapped in `<tender_corpus>` or `<calculation>` and defused, so the prefix's
untrusted-data rule covers it. Inputs are validated with the tools' own Pydantic models even though the API enforces
the strict schemas: a fake or replayed response must not reach a tool unchecked.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from anbud.granskning.catalog import Catalog, CatalogItem, tokens
from anbud.granskning.contracts import CalcPost
from anbud.granskning.corpus import Corpus
from anbud.granskning.prefix import block_line, defuse, post_line, provenance
from anbud.granskning.search import SearchIndex
from anbud.tools import INPUT_MODELS

READ_TOOLS = ("search_blocks", "read_blocks", "outline", "calc_posts", "law_lookup", "view_tile", "catalog_search")
SEARCH_LIMIT = 20
CALC_LIMIT = 150
CATALOG_LIMIT = 25
LAW_NOT_AVAILABLE = (
    "law_lookup is not available yet: the harness has no public-law corpus. Cite the statute and section in "
    "reference_rule as you know them, and say in the claim that the wording was not checked."
)


@dataclass
class ToolOutput:
    content: str | list[dict[str, Any]]
    result_ids: list[str] = field(default_factory=list)
    error: str | None = None  # a short machine reason; the content carries the model-facing text


class Toolbox:
    def __init__(
        self,
        corpus: Corpus,
        calc: list[CalcPost],
        tile_loader: Callable[[str], bytes] | None = None,
        index: SearchIndex | None = None,
        catalog: Catalog | None = None,
    ) -> None:
        self.corpus = corpus
        self.calc = calc
        self.catalog = catalog
        self.tile_loader = tile_loader
        self.index = index or SearchIndex(corpus)
        self.doc_roles: dict[str, str] = {}  # doc_id -> role, from the tender map once it exists

    def set_roles(self, roles: dict[str, str]) -> None:
        self.doc_roles = dict(roles)

    def run(self, tool: str, raw_input: Any) -> ToolOutput:
        if tool not in READ_TOOLS:
            return ToolOutput(f"{tool} is not a read tool", error="unknown_tool")
        try:
            args = INPUT_MODELS[tool].model_validate(raw_input)
        except ValidationError as exc:
            reasons = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in exc.errors())
            return ToolOutput(f"invalid input for {tool}: {reasons}", error="invalid_input")
        return getattr(self, f"_{tool}")(args)

    # Documents

    def _resolve_doc(self, ref: str) -> str | None:
        ref = ref.strip()
        known = [d["doc_id"] for d in self.corpus.documents] or sorted({b.doc_id for b in self.corpus.blocks.values()})
        if ref in known:
            return ref
        matches = [d for d in known if d.startswith(ref)] if len(ref) >= 4 else []
        return matches[0] if len(matches) == 1 else None

    def _search_blocks(self, args: Any) -> ToolOutput:
        doc_ids: set[str] | None = None
        note = ""
        if args.roles:
            wanted = {r.casefold() for r in args.roles}
            if self.doc_roles:
                doc_ids = {d for d, role in self.doc_roles.items() if role.casefold() in wanted}
                if not doc_ids:
                    note = "No document has those roles; searched every document.\n"
                    doc_ids = None
            else:
                note = "Document roles aren't known yet; searched every document.\n"
        hits = self.index.search(args.query, doc_ids, SEARCH_LIMIT)
        if not hits:
            return ToolOutput(f"{note}No blocks match.")
        lines = [f'<tender_corpus results="{len(hits)}">']
        ids = []
        for hit in hits:
            snippet = defuse(" ".join(hit.snippet.split()))
            trade = self.corpus.excluded.get(hit.doc_id)
            outside = f" [excluded: {defuse(trade)}]" if trade is not None else ""
            if hit.block_id is not None and (block := self.corpus.get(hit.block_id)) is not None:
                lines.append(f"{block.id} {provenance(block)}: {snippet}{outside}")
                ids.append(hit.block_id)
            else:
                lines.append(f"outline {hit.doc_id}: {snippet}")
        lines.append("</tender_corpus>")
        return ToolOutput(note + "\n".join(lines), ids)

    def _read_blocks(self, args: Any) -> ToolOutput:
        requested = list(dict.fromkeys(args.ids))
        found = [b for i in requested if (b := self.corpus.get(i)) is not None]
        missing = [i for i in requested if self.corpus.get(i) is None]
        lines = []
        if found:
            lines.append("<tender_corpus>")
            for block in found:
                lines.append(f"{block_line(block)}  (doc {block.doc_id})")
            lines.append("</tender_corpus>")
        if missing:
            lines.append("Unknown block ids: " + ", ".join(defuse(m) for m in missing))
        return ToolOutput(
            "\n".join(lines), [b.id for b in found], error="unknown_ids" if missing and not found else None
        )

    def _outline(self, args: Any) -> ToolOutput:
        doc_id = None
        if args.doc_id is not None:
            doc_id = self._resolve_doc(args.doc_id)
            if doc_id is None:
                return ToolOutput(f"Unknown document id: {defuse(args.doc_id)}", error="unknown_doc")
        entries = [e for e in self.corpus.outline if doc_id is None or e.doc_id == doc_id]
        if not entries:
            return ToolOutput("No headings found.")
        lines = ["<tender_corpus>"]
        current = None
        for entry in entries:
            if entry.doc_id != current:
                current = entry.doc_id
                lines.append(f"doc {current}")
            where = f"p{entry.page}" if entry.page is not None else (defuse(entry.sheet) if entry.sheet else "-")
            title = defuse(f"{entry.number} {entry.title}" if entry.number else entry.title)
            ref = entry.block_id or "-"
            lines.append(f"{'  ' * max(0, entry.level - 1)}{title}  [{ref} {where}]")
        lines.append("</tender_corpus>")
        return ToolOutput("\n".join(lines), [e.block_id for e in entries if e.block_id])

    # Calc

    def _calc_posts(self, args: Any) -> ToolOutput:
        posts = self.calc
        if not posts:
            return ToolOutput("No calculation was supplied for this tender.", error="no_calc")
        if args.filter:
            words = [w for w in args.filter.casefold().split() if w]

            def hay(p: CalcPost) -> str:
                return f"{p.id} {p.section or ''} {p.text} {p.unit or ''}".casefold()

            matched = [p for p in posts if all(w in hay(p) for w in words)]
            if not matched:
                matched = [p for p in posts if any(w in hay(p) for w in words)]
            posts = matched
        if not posts:
            return ToolOutput("No calc posts match.")
        shown = posts[:CALC_LIMIT]
        lines = [f'<calculation matches="{len(posts)}" shown="{len(shown)}">']
        lines.extend(post_line(p) for p in shown)
        lines.append("</calculation>")
        return ToolOutput("\n".join(lines), [p.id for p in shown])

    # Catalog

    def _catalog_search(self, args: Any) -> ToolOutput:
        if self.catalog is None or not self.catalog.items:
            return ToolOutput(
                "No catalog is loaded for this run. Name the item in money_basis.item and leave catalog_id out.",
                error="no_catalog",
            )
        found = self.catalog.search(args.query)
        if not found:  # every word didn't match: fall back to items that hold any of them
            words = {w for w in tokens(args.query) if len(w) > 2}  # "el", "i", "av" would match half the catalog
            found = [i for i in self.catalog.items if words & tokens(i.name)]
        if not found:
            return ToolOutput(
                "No catalog item matches. Try other words; if nothing fits, name the item in money_basis.item and "
                "leave catalog_id out: the finding stays unpriced and is still reported."
            )
        shown = found[:CATALOG_LIMIT]
        lines = [f'<catalog matches="{len(found)}" shown="{len(shown)}">', *map(_catalog_line, shown), "</catalog>"]
        return ToolOutput("\n".join(lines), [i.id for i in shown])

    # Law and tiles

    def _law_lookup(self, args: Any) -> ToolOutput:
        return ToolOutput(LAW_NOT_AVAILABLE, error="not_available")

    def _view_tile(self, args: Any) -> ToolOutput:
        tile = self.corpus.tiles.get(args.tile.strip())
        if tile is None:
            doc_id = self._resolve_doc(args.sheet)
            if doc_id is not None:
                wanted = args.tile.strip().casefold()
                for candidate in self.corpus.tiles.values():
                    if candidate.doc_id == doc_id and (
                        candidate.id.casefold().endswith(wanted) or wanted == str(candidate.index)
                    ):
                        tile = candidate
                        break
        if tile is None:
            return ToolOutput(
                f"Unknown tile {defuse(args.tile)} on {defuse(args.sheet)}. Tile ids are listed in <documents>.",
                error="unknown_tile",
            )
        if self.tile_loader is None:
            return ToolOutput("Tile images aren't available in this run.", [tile.id], error="no_tiles")
        try:
            data = self.tile_loader(tile.blob)
        except (OSError, ValueError, RuntimeError):
            return ToolOutput("The tile image couldn't be read.", [tile.id], error="tile_unreadable")
        caption = (
            f"<tender_corpus>tile {tile.id}: doc {tile.doc_id}, page {tile.page}, tile {tile.index}, "
            f"{tile.reason}, {tile.dpi} dpi, box {tile.x0},{tile.y0}-{tile.x1},{tile.y1} px</tender_corpus>"
        )
        image = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(data).decode()},
        }
        return ToolOutput([{"type": "text", "text": caption}, image], [tile.id])


def _catalog_line(item: CatalogItem) -> str:
    """Id, name, unit and category. Never hours or prices."""
    parts = [f"id={item.id}", defuse(item.name), f"unit={defuse(item.unit)}"]
    if item.category:
        parts.append(f"category={defuse(item.category)}")
    return " | ".join(parts)
