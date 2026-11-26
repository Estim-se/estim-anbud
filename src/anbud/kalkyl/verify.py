"""Code checks every R2 record before anything is counted or priced (#73 step 4).

- Every citation names a block of the chunk (or its context rows) in one document, the block has text, and the quote,
  normalised with `anbud.prep.text.normalise`, lies inside the normalised block text.
- A quantity the record says is written must be written in a quote: `stated` needs its number, `per_object` its
  number unless it is 1, `list_row` its number unless it is 1. A record without a number in the text is at most an
  interpretation, and `missing` has no quantity at all.
- The structure has to fit: objects carry an object kind, `per_object` names the kind it multiplies by.
- A catalog id that isn't in the catalog is dropped, not guessed: the post goes to R3 or stays unmapped.

A record that fails is never priced; its reason goes back to the model once, and after that to the run's rejects.
"""

from __future__ import annotations

from pydantic import ValidationError

from anbud.granskning.corpus import Corpus
from anbud.kalkyl.lines import Chunk, numbers_in
from anbud.kalkyl.records import Level, QuantityRecord, SetAside
from anbud.prep.text import normalise


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


def parse_record(raw: object) -> tuple[QuantityRecord | None, str | None]:
    try:
        return QuantityRecord.model_validate(raw), None
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) or "record" for e in exc.errors()})
        return None, f"doesn't match the record fields ({', '.join(fields)})"


def check_record(record: QuantityRecord, chunk: Chunk, corpus: Corpus) -> str | None:
    """Why the record fails, or None if it passes."""
    docs = set()
    for c in record.citations:
        block = corpus.get(c.block_id)
        if block is None or c.block_id not in chunk.citable:
            return f"block {c.block_id} isn't in this chunk"
        docs.add(block.doc_id)
        text, quote = normalise(block.text), normalise(c.quote)
        if not text:
            return f"block {c.block_id} has no text, so it can't be quoted"
        if not quote:
            return f"the quote from block {c.block_id} is empty"
        if quote not in text:
            return f"the quote isn't in block {c.block_id} as written"
    if len(docs) > 1:
        return "citations span more than one document"
    written = set().union(*(numbers_in(normalise(c.quote)) for c in record.citations))
    has = any(_close(record.quantity, n) for n in written) if record.quantity is not None else False
    src = record.quantity_source
    if record.record_kind == "object" and record.object_kind is None:
        return "an object record needs object_kind"
    if src == "stated":
        if record.quantity is None:
            return "quantity_source stated needs the quantity"
        if not has:
            return f"the quantity {record.quantity:g} isn't written in the quotes"
    elif src == "per_object":
        if record.per_object is None:
            return "quantity_source per_object needs per_object"
        if record.quantity is None:
            return "quantity_source per_object needs the quantity per object (1 if the text implies one)"
        if not has and not _close(record.quantity, 1):
            return f"the quantity per object {record.quantity:g} isn't written in the quotes"
    elif src == "list_row":
        if record.record_kind != "object":
            return "list_row is for object records; use stated for a requirement row"
        if record.quantity is not None and not has and not _close(record.quantity, 1):
            return f"the row count {record.quantity:g} isn't written in the quotes"
    elif src == "missing" and record.quantity is not None:
        return "quantity_source missing can't carry a quantity"
    return None


def level_of(record: QuantityRecord, corpus: Corpus) -> Level:
    if any((b := corpus.get(c.block_id)) is not None and b.kind == "tile_text" for c in record.citations):
        return "visual"
    written = set().union(*(numbers_in(normalise(c.quote)) for c in record.citations))
    if record.quantity is not None and any(_close(record.quantity, n) for n in written):
        return "exact"
    if record.quantity_source == "list_row" and record.quantity in (None, 1):
        return "exact"  # the row itself is the evidence of one object
    return "interpretation"


def without_unknown_catalog_id(record: QuantityRecord, catalog_ids: set[str]) -> QuantityRecord:
    if record.catalog_id is not None and record.catalog_id not in catalog_ids:
        return record.model_copy(update={"catalog_id": None})
    return record


def parse_set_aside(raw: object, chunk: Chunk) -> tuple[SetAside | None, str | None]:
    try:
        item = SetAside.model_validate(raw)
    except ValidationError:
        return None, "a set_aside entry doesn't match its fields"
    unknown = [b for b in item.block_ids if b not in chunk.block_ids]
    if unknown:
        return None, f"set_aside names blocks outside this chunk ({len(unknown)})"
    return item, None
