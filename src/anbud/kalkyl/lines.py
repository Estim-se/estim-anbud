"""Quantity lines, heading paths and chunks: everything about the corpus that code decides before a model reads it.

- **A quantity line is found by its form, never its words:** a table or sheet row with a numeric cell (a bare number,
  or a number with a unit), or a paragraph or tile line with a number next to a unit. Headings never count. The unit
  tokens are generic units of measure and count, not vocabulary of any tender.
- **Heading paths** come from the outline: every block inherits the titles above it in its own document.
- **Chunks** are cut at headings inside one document and sized by what they will make the model write (quantity lines)
  and read (characters), per #73. A chunk that starts in the middle of a sheet also shows that sheet's first row as
  context, so the model knows the columns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from anbud.granskning.corpus import Corpus
from anbud.models import Block

_NUMBER = r"\d{1,3}(?:[  ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?"
# Units of measure and count. Lower-cased before matching.
_UNITS = r"st|st\.|styck|stycken|pcs|m|lm|m2|m²|kvm|m3|m³|km|kg|h|tim|lgh|rok|pkt|sats|set"
_CELL_NUMBER = re.compile(rf"^[-+]?(?:{_NUMBER})\s?(?:{_UNITS})?$")
_NUMBER_UNIT = re.compile(rf"(?<![\w.,])(?:{_NUMBER})\s?(?:{_UNITS})(?![\w²³])")
# A number not glued to a letter, digit or decimal sign before it: a type code like L12 or a unit like m2 adds none.
_WRITTEN_NUMBER = re.compile(rf"(?<![\w.,])(?:{_NUMBER})")
_ROW_KINDS = ("table_row", "sheet_row")
_LINE_KINDS = ("paragraph", "tile_text")


def is_quantity_line(block: Block) -> bool:
    text = block.text.strip()
    if not text or block.kind == "heading":
        return False
    if block.kind in _ROW_KINDS:
        cells = [c.strip().lower() for c in text.split(" | ")]
        return any(_CELL_NUMBER.match(c) for c in cells if c)
    if block.kind in _LINE_KINDS:
        return bool(_NUMBER_UNIT.search(text.lower()))
    return False


def numbers_in(text: str) -> set[float]:
    """Every number written in the text, as floats: `1 200` and `1200` are one number, `2,5` and `2.5` another. Digits
    glued to a letter before them (a type code, `m2`) aren't numbers."""
    found: set[float] = set()
    for m in _WRITTEN_NUMBER.finditer(text):
        grouped = m.group(0)
        parts = [grouped, *re.split(r"[  ]", grouped)] if re.search(r"[  ]", grouped) else [grouped]
        for part in parts:  # "2 120" may be two numbers as well as one; both readings count
            found.add(float(re.sub(r"[  ]", "", part).replace(",", ".")))
    return found


def heading_paths(corpus: Corpus) -> dict[str, tuple[str, ...]]:
    """Each block's heading path in its document, from the outline. A heading's own path ends with itself."""
    starts = {o.block_id: o for o in corpus.outline if o.block_id}
    paths: dict[str, tuple[str, ...]] = {}
    stacks: dict[str, list[tuple[int, str]]] = {}
    for block_id in corpus.order:
        block = corpus.blocks[block_id]
        stack = stacks.setdefault(block.doc_id, [])
        entry = starts.get(block_id)
        if entry is not None:
            while stack and stack[-1][0] >= entry.level:
                stack.pop()
            stack.append((entry.level, entry.title))
        paths[block_id] = tuple(title for _, title in stack)
    return paths


@dataclass
class Chunk:
    id: str
    doc_id: str
    block_ids: list[str]
    context_ids: list[str] = field(default_factory=list)  # shown for orientation; covered by their own chunk
    quantity_ids: list[str] = field(default_factory=list)
    chars: int = 0

    @property
    def citable(self) -> set[str]:
        return set(self.block_ids) | set(self.context_ids)


def _is_boundary(block: Block, outline_starts: set[str]) -> bool:
    return block.kind == "heading" or block.id in outline_starts


def make_chunks(corpus: Corpus, max_chars: int, max_quantity_lines: int) -> list[Chunk]:
    """Chunks in reading order, never across documents. A chunk is closed when the next block would pass either cap;
    the cut moves back to the last heading in the chunk when that keeps at least a third of it."""
    starts = {o.block_id for o in corpus.outline if o.block_id}
    groups: list[list[str]] = []
    current: list[str] = []
    chars = lines = 0

    def close(upto: int) -> None:
        nonlocal current, chars, lines
        groups.append(current[:upto])
        current = current[upto:]
        chars = sum(len(corpus.blocks[b].text) for b in current)
        lines = sum(is_quantity_line(corpus.blocks[b]) for b in current)

    prev_doc = None
    for block_id in corpus.order:
        block = corpus.blocks[block_id]
        if current and block.doc_id != prev_doc:
            close(len(current))
        prev_doc = block.doc_id
        q = is_quantity_line(block)
        while current and (chars + len(block.text) > max_chars or lines + q > max_quantity_lines):
            cut = len(current)
            if not _is_boundary(block, starts):
                heads = [i for i, b in enumerate(current) if i > 0 and _is_boundary(corpus.blocks[b], starts)]
                if heads and heads[-1] >= len(current) // 3:
                    cut = heads[-1]
            close(cut)  # what is left after a cut at a heading may still be too full: then it closes too
        current.append(block_id)
        chars += len(block.text)
        lines += q
    if current:
        groups.append(current)

    first_row: dict[tuple[str, str], str] = {}
    for block_id in corpus.order:
        b = corpus.blocks[block_id]
        if b.kind == "sheet_row" and b.sheet is not None:
            first_row.setdefault((b.doc_id, b.sheet), block_id)
    chunks = []
    for n, ids in enumerate(groups, 1):
        head = corpus.blocks[ids[0]]
        context = []
        if head.kind == "sheet_row" and head.sheet is not None:
            top = first_row.get((head.doc_id, head.sheet))
            if top and top not in ids:
                context.append(top)
        chunks.append(
            Chunk(
                id=f"c{n:03d}",
                doc_id=head.doc_id,
                block_ids=ids,
                context_ids=context,
                quantity_ids=[b for b in ids if is_quantity_line(corpus.blocks[b])],
                chars=sum(len(corpus.blocks[b].text) for b in ids),
            )
        )
    return chunks
