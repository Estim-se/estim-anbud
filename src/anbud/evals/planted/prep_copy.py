"""A prep output loaded for patching, and written back as a separate copy. The original folder is only ever read."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anbud.models import Block
from anbud.prep.model import MaskedSpan, OutlineEntry
from anbud.prep.text import DOC_PREFIX, TEXT_PREFIX, text_hash
from anbud.store import RunStore, canonical

PREP_FILES = ("documents.json", "blocks.jsonl", "outline.json", "tiles.json", "masking.jsonl", "summary.json")


def _lines(path: Path) -> list[str]:
    return [line for line in path.read_text().splitlines() if line] if path.exists() else []


@dataclass
class Prep:
    blocks: list[Block]
    outline: list[OutlineEntry]
    documents: list[dict[str, Any]]
    tiles: bytes
    masking: list[MaskedSpan]
    summary: dict[str, Any]
    _ids: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, prep_dir: Path) -> Prep:
        blocks = [Block.model_validate_json(line) for line in _lines(prep_dir / "blocks.jsonl")]
        outline = [OutlineEntry.model_validate(o) for o in json.loads((prep_dir / "outline.json").read_text())]
        return cls(
            blocks=blocks,
            outline=outline,
            documents=json.loads((prep_dir / "documents.json").read_text()),
            tiles=(prep_dir / "tiles.json").read_bytes(),
            masking=[MaskedSpan.model_validate_json(line) for line in _lines(prep_dir / "masking.jsonl")],
            summary=json.loads((prep_dir / "summary.json").read_text()),
            _ids={b.id for b in blocks},
        )

    @classmethod
    def empty(cls) -> Prep:
        """A tender with no documents at all: the calc-only arm."""
        summary = {
            "documents": 0,
            "documents_by_kind": {},
            "skipped": 0,
            "pages_by_kind": {},
            "sheets": 0,
            "blocks_by_kind": {},
            "hidden_blocks": 0,
            "block_chars": 0,
            "outline_entries": 0,
            "tiles": 0,
            "masked_by_kind": {},
            "furniture_dropped": 0,
            "cache_hits": 0,
            "links_skipped": 0,
            "hidden_files_skipped": 0,
        }
        return cls([], [], [], b"[]", [], summary)

    # Lookups

    def masked_blocks(self) -> set[str]:
        return {m.block_id for m in self.masking if m.block_id}

    def doc_blocks(self, doc_id: str) -> list[Block]:
        return [b for b in self.blocks if b.doc_id == doc_id]

    def doc_prefix(self, doc_id: str) -> str:
        for block in self.blocks:
            if block.doc_id == doc_id:
                return block.id.split("-")[0]
        for doc in self.documents:
            if doc.get("doc_id") == doc_id:
                return str(doc["sha256"])[:DOC_PREFIX]
        raise KeyError(doc_id)

    def new_id(self, doc_id: str, text: str) -> str:
        """A block id the way prep makes one: the file's prefix plus the text's hash, with a repeat suffix if taken."""
        base = f"{self.doc_prefix(doc_id)}-{text_hash(text)[:TEXT_PREFIX]}"
        candidate, n = base, 1
        while candidate in self._ids:
            n += 1
            candidate = f"{base}-{n}"
        return candidate

    # Edits

    def insert_after(self, anchor_id: str | None, block: Block) -> None:
        """Insert after the anchor, or at the end of the block's document when there is no anchor."""
        if anchor_id is None:
            last = max((i for i, b in enumerate(self.blocks) if b.doc_id == block.doc_id), default=len(self.blocks) - 1)
            pos = last + 1
        else:
            pos = next(i for i, b in enumerate(self.blocks) if b.id == anchor_id) + 1
        self.blocks.insert(pos, block)
        self._ids.add(block.id)

    def replace(self, block_id: str, new: Block) -> None:
        i = next(i for i, b in enumerate(self.blocks) if b.id == block_id)
        self.blocks[i] = new
        self._ids.discard(block_id)
        self._ids.add(new.id)
        self.outline = [
            o.model_copy(update={"block_id": new.id}) if o.block_id == block_id else o for o in self.outline
        ]

    def delete(self, block_ids: set[str]) -> None:
        self.blocks = [b for b in self.blocks if b.id not in block_ids]
        self._ids -= block_ids
        self.outline = [o for o in self.outline if o.block_id not in block_ids]
        self.masking = [m for m in self.masking if m.block_id not in block_ids]

    # Output

    def _recount(self) -> None:
        per_doc = Counter(b.doc_id for b in self.blocks)
        masked = Counter(m.doc_id for m in self.masking)
        self.documents = [
            {**d, "blocks": per_doc.get(d["doc_id"], 0), "masked": masked.get(d["doc_id"], 0)} for d in self.documents
        ]
        self.summary = {
            **self.summary,
            "blocks_by_kind": dict(Counter(b.kind for b in self.blocks)),
            "hidden_blocks": sum(b.hidden for b in self.blocks),
            "block_chars": sum(len(b.text) for b in self.blocks),
            "outline_entries": len(self.outline),
            "masked_by_kind": dict(Counter(m.kind for m in self.masking)),
        }

    def write(self, store: RunStore, out_dir: Path) -> None:
        self._recount()
        store.write_file(out_dir / "documents.json", canonical(self.documents))
        store.write_file(out_dir / "blocks.jsonl", b"".join(b.model_dump_json().encode() + b"\n" for b in self.blocks))
        store.write_file(out_dir / "outline.json", canonical([o.model_dump() for o in self.outline]))
        store.write_file(out_dir / "tiles.json", self.tiles)
        store.write_file(
            out_dir / "masking.jsonl", b"".join(m.model_dump_json().encode() + b"\n" for m in self.masking)
        )
        store.write_file(out_dir / "summary.json", json.dumps(self.summary, indent=2, sort_keys=True).encode())


def copy_prep(store: RunStore, src: Path, out_dir: Path) -> None:
    """The prep output copied byte for byte: the unplanted arm reads exactly what prep wrote."""
    for name in PREP_FILES:
        if (src / name).exists():
            store.write_file(out_dir / name, (src / name).read_bytes())
