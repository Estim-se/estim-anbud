"""The prepared tender as one in-memory corpus: blocks by id in reading order, the outline and the tiles.

`order` is what the models read; `all_order` is every block, the same unless the selection (`anbud.selection`) left
another trade's documents out of `order`. Search and neighbours go over `all_order`, so excluded documents stay
searchable; `excluded` maps each excluded document to its trade.

Search (FTS5) lives with the tool loop; this module only loads and looks up.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from anbud.models import Block
from anbud.prep.model import OutlineEntry, Tile


@dataclass
class Corpus:
    blocks: dict[str, Block]
    order: list[str]
    outline: list[OutlineEntry] = field(default_factory=list)
    tiles: dict[str, Tile] = field(default_factory=dict)
    documents: list[dict] = field(default_factory=list)
    all_order: list[str] = field(default_factory=list)
    excluded: dict[str, str] = field(default_factory=dict)  # doc_id -> trade, for documents outside `order`
    search_only: dict[str, str] = field(default_factory=dict)  # doc_id -> why the prefix holds only its outline

    def __post_init__(self) -> None:
        if not self.all_order:
            self.all_order = list(self.order)

    @classmethod
    def load(cls, prep_dir: Path) -> Corpus:
        blocks = [
            Block.model_validate_json(line) for line in (prep_dir / "blocks.jsonl").read_text().splitlines() if line
        ]
        outline = [OutlineEntry.model_validate(o) for o in json.loads((prep_dir / "outline.json").read_text())]
        tiles = [Tile.model_validate(t) for t in json.loads((prep_dir / "tiles.json").read_text())]
        documents = json.loads((prep_dir / "documents.json").read_text())
        return cls({b.id: b for b in blocks}, [b.id for b in blocks], outline, {t.id: t for t in tiles}, documents)

    def get(self, block_id: str) -> Block | None:
        return self.blocks.get(block_id)

    def neighbours(self, block_id: str, n: int = 1) -> list[Block]:
        """The block with up to n blocks either side of it in the same document."""
        if block_id not in self.blocks:
            return []
        i = self.all_order.index(block_id)
        doc = self.blocks[block_id].doc_id
        window = self.all_order[max(0, i - n) : i + n + 1]
        return [self.blocks[b] for b in window if self.blocks[b].doc_id == doc]
