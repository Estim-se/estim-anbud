"""Document plants: patches over the prepared blocks, applied to a working copy. Dev tenders only.

- `contradicting_quantity`: where two blocks state the same quantity, one of them now states more; with no such pair,
  a block is restated with a larger number in another document;
- `vite_clause` / `index_clause`: a clause in Estim's own wording, inserted where the contract terms sit;
- `delete_section`: a numbered section another block refers to is deleted, leaving the reference dangling;
- `drawing_note_conflict`: a drawing gets a note that states a different quantity from the specification.

Changed and inserted blocks get ids the way prep makes them, from their text. Blocks with masked spans are never
changed, so the masking report stays true. Plants never share a block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from random import Random

from anbud.evals.planted.key import ALL_BASIS, EXPECTED_CLAIMS, Change, Expected, Location, PlantKind
from anbud.evals.planted.prep_copy import Prep
from anbud.models import Block

QUANTITY = re.compile(
    r"(?<![\d.,])(\d{1,4})\s?(st|m|lgh|lägenheter|uttag|armaturer|meter|rum|våningar)\b", re.IGNORECASE
)
_CONTRACT = re.compile(
    r"\bAF[A-D]|administrativa föreskrifter|kontraktssumma|entreprenadtid|ersättning|betalning|garanti|"
    r"färdigställ|tidplan",
    re.IGNORECASE,
)
_TEXT_KINDS = ("paragraph", "table_row", "sheet_row")
_ANCHOR = {
    "vite_clause": re.compile(r"färdigställ|entreprenadtid|tidplan|\btider\b|slutbesiktning", re.IGNORECASE),
    "index_clause": re.compile(r"ersättning|betalning|kontraktssumma|á-pris|à-pris|pris", re.IGNORECASE),
}
_ALREADY = {"vite_clause": re.compile(r"\bvite", re.IGNORECASE), "index_clause": re.compile(r"\bindex", re.IGNORECASE)}
_VITE = (
    "Om entreprenaden färdigställs efter avtalad tid betalar entreprenören vite med {pct} procent av "
    "kontraktssumman per påbörjad vecka.",
    "Vid försenat färdigställande utgår vite om {kr} kronor per påbörjad kalendervecka.",
)
_INDEX = (
    "Kontraktssumman är fast och regleras inte med index under entreprenadtiden.",
    "Indexreglering av ersättningen medges inte, oavsett entreprenadtidens längd.",
)
_REFERENCE = "(?<![\\w.]){}(?![\\w]|\\.\\d)"
MAX_SECTION_BLOCKS = 25


@dataclass
class DocPlant:
    kind: PlantKind
    changes: list[Change]
    expected: Expected
    planted: Location
    unplanted: Location
    used: set[str] = field(default_factory=set)


def _expected(kind: PlantKind, **kw: object) -> Expected:
    basis = {
        "contradicting_quantity": ["quantity_gap"],
        "drawing_note_conflict": ["quantity_gap"],
        "vite_clause": ["contract_formula"],
        "index_clause": ["contract_formula"],
        "delete_section": ALL_BASIS,
    }[kind]
    return Expected(claim_types=EXPECTED_CLAIMS[kind], basis_kinds=basis, **kw)  # type: ignore[arg-type]


def _free(prep: Prep, used: set[str]) -> list[Block]:
    masked = prep.masked_blocks()
    return [b for b in prep.blocks if b.id not in used and b.id not in masked and not b.hidden]


def _drawing_docs(prep: Prep) -> list[str]:
    docs = {b.doc_id for b in prep.blocks if b.kind == "tile_text"}
    for doc in prep.documents:
        if any(p.get("kind") == "drawing" for p in doc.get("pages", [])):
            docs.add(doc["doc_id"])
    return sorted(docs)


def _bigger(n: int, rng: Random) -> int:
    return n + max(1, round(n * rng.uniform(0.2, 0.5)))


def contradicting_quantity(prep: Prep, rng: Random, used: set[str]) -> DocPlant | None:
    drawings = set(_drawing_docs(prep))
    blocks = [b for b in _free(prep, used) if b.kind in _TEXT_KINDS and b.doc_id not in drawings]
    stated: dict[tuple[int, str], list[tuple[Block, re.Match[str]]]] = {}
    for block in blocks:
        for m in QUANTITY.finditer(block.text):
            stated.setdefault((int(m.group(1)), m.group(2).lower()), []).append((block, m))
    pairs = sorted((k, v) for k, v in stated.items() if len({b.id for b, _ in v}) >= 2 and k[0] > 0)
    if pairs:
        _, hits = pairs[rng.randrange(len(pairs))]
        (keep, _), (target, m) = hits[0], next(h for h in hits if h[0].id != hits[0][0].id)
        n = int(m.group(1))
        n2 = _bigger(n, rng)
        text = target.text[: m.start(1)] + str(n2) + target.text[m.end(1) :]
        new = target.model_copy(update={"id": prep.new_id(target.doc_id, text), "text": text})
        prep.replace(target.id, new)
        change = Change(
            target="block",
            action="replaced",
            doc_id=target.doc_id,
            block_id=target.id,
            new_block_id=new.id,
            before=n,
            after=n2,
            before_text=target.text,
            after_text=text,
        )
        return DocPlant(
            "contradicting_quantity",
            [change],
            _expected("contradicting_quantity", quantity=float(n2 - n)),
            Location(blocks=[new.id, keep.id]),
            Location(blocks=[target.id, keep.id]),
            {target.id, keep.id, new.id},
        )
    singles = [(b, m) for v in stated.values() for b, m in v if int(m.group(1)) > 0]
    docs = sorted({b.doc_id for b in blocks})
    if not singles or len(docs) < 2:
        return None
    source, m = singles[rng.randrange(len(singles))]
    others = [b for b in blocks if b.doc_id != source.doc_id and b.kind == "paragraph"]
    if not others:
        return None
    anchor = others[rng.randrange(len(others))]
    n = int(m.group(1))
    n2 = _bigger(n, rng)
    text = source.text[: m.start(1)] + str(n2) + source.text[m.end(1) :]
    new = Block(
        id=prep.new_id(anchor.doc_id, text),
        doc_id=anchor.doc_id,
        kind="paragraph",
        text=text,
        page=anchor.page,
        line=anchor.line,
    )
    prep.insert_after(anchor.id, new)
    change = Change(
        target="block",
        action="inserted",
        doc_id=anchor.doc_id,
        block_id=anchor.id,
        new_block_id=new.id,
        before=n,
        after=n2,
        after_text=text,
    )
    return DocPlant(
        "contradicting_quantity",
        [change],
        _expected("contradicting_quantity", quantity=float(n2 - n)),
        Location(blocks=[new.id, source.id]),
        Location(blocks=[source.id]),
        {source.id, anchor.id, new.id},
    )


def _contract_doc(prep: Prep, blocks: list[Block]) -> str | None:
    scores: dict[str, int] = {}
    for block in blocks:
        if block.kind in _TEXT_KINDS:
            scores[block.doc_id] = scores.get(block.doc_id, 0) + bool(_CONTRACT.search(block.text))
    if not scores:
        return None
    return max(sorted(scores), key=lambda d: scores[d])


def clause(kind: PlantKind, prep: Prep, rng: Random, used: set[str]) -> DocPlant | None:
    if any(_ALREADY[kind].search(b.text) for b in prep.blocks):
        return None  # the tender already has one; a run would flag that, not the plant
    blocks = _free(prep, used)
    doc = _contract_doc(prep, blocks)
    if doc is None:
        return None
    in_doc = [b for b in blocks if b.doc_id == doc and b.kind in _TEXT_KINDS]
    anchors = [b for b in in_doc if _ANCHOR[kind].search(b.text)] or in_doc[-1:]
    anchor = anchors[rng.randrange(len(anchors))]
    if kind == "vite_clause":
        template = _VITE[rng.randrange(len(_VITE))]
        pct = rng.choice(["0,5", "1", "1,5"])
        kr = rng.choice([25_000, 50_000, 75_000])
        text = template.format(pct=pct, kr=f"{kr:,}".replace(",", " "))
        value = float(pct.replace(",", ".")) if "{pct}" in template else float(kr)
        expected = _expected(kind, formula="vite_per_week")
    else:
        text = _INDEX[rng.randrange(len(_INDEX))]
        value = None
        expected = _expected(kind, formula="index_drift")
    new = Block(
        id=prep.new_id(doc, text),
        doc_id=doc,
        kind="paragraph",
        text=text,
        page=anchor.page,
        line=anchor.line,
    )
    prep.insert_after(anchor.id, new)
    change = Change(
        target="block",
        action="inserted",
        doc_id=doc,
        block_id=anchor.id,
        new_block_id=new.id,
        after=value,
        after_text=text,
    )
    return DocPlant(kind, [change], expected, Location(blocks=[new.id]), Location(anywhere=True), {anchor.id, new.id})


def delete_section(prep: Prep, rng: Random, used: set[str]) -> DocPlant | None:
    heads = {o.block_id: o for o in prep.outline if o.block_id and o.number and not re.fullmatch(r"\d+", o.number)}
    order = {b.id: i for i, b in enumerate(prep.blocks)}
    outlined = {o.block_id: o for o in prep.outline if o.block_id}
    options = []
    for block_id, entry in sorted(heads.items(), key=lambda kv: order.get(kv[0], -1)):
        if block_id not in order or block_id in used:
            continue
        start = order[block_id]
        section = [prep.blocks[start]]
        for block in prep.blocks[start + 1 :]:
            if block.doc_id != entry.doc_id:
                break
            other = outlined.get(block.id)
            if other is not None and other.level <= entry.level:
                break
            section.append(block)
        ids = {b.id for b in section}
        if not 2 <= len(section) <= MAX_SECTION_BLOCKS or ids & used:
            continue
        ref = re.compile(_REFERENCE.format(re.escape(entry.number or "")))
        referring = [b.id for b in prep.blocks if b.id not in ids and ref.search(b.text)]
        if referring:
            options.append((section, referring))
    if not options:
        return None
    section, referring = options[rng.randrange(len(options))]
    ids = {b.id for b in section}
    prep.delete(ids)
    changes = [
        Change(target="block", action="deleted", doc_id=b.doc_id, block_id=b.id, before_text=b.text) for b in section
    ]
    return DocPlant(
        "delete_section",
        changes,
        _expected("delete_section"),
        Location(blocks=referring),
        Location(blocks=[*referring, *[b.id for b in section]]),
        ids | set(referring),
    )


def drawing_note_conflict(prep: Prep, rng: Random, used: set[str]) -> DocPlant | None:
    drawings = _drawing_docs(prep)
    if not drawings:
        return None
    specs = [
        (b, m)
        for b in _free(prep, used)
        if b.kind in _TEXT_KINDS and b.doc_id not in drawings
        for m in QUANTITY.finditer(b.text)
        if int(m.group(1)) > 0
    ]
    if not specs:
        return None
    spec, m = specs[rng.randrange(len(specs))]
    doc = drawings[rng.randrange(len(drawings))]
    n = int(m.group(1))
    n2 = _bigger(n, rng)
    sentence = next(s for s in re.split(r"(?<=[.!?])\s+", spec.text) if m.group(0) in s)
    sentence = sentence.replace(m.group(0), f"{n2} {m.group(2)}", 1)[:200]
    text = "Anm: " + sentence
    in_doc = prep.doc_blocks(doc)
    kind = "tile_text" if any(b.kind == "tile_text" for b in in_doc) else "paragraph"
    last = in_doc[-1] if in_doc else None
    new = Block(
        id=prep.new_id(doc, text),
        doc_id=doc,
        kind=kind,  # type: ignore[arg-type]
        text=text,
        page=last.page if last else 1,
    )
    prep.insert_after(last.id if last else None, new)
    change = Change(
        target="block",
        action="inserted",
        doc_id=doc,
        block_id=last.id if last else None,
        new_block_id=new.id,
        before=n,
        after=n2,
        after_text=text,
    )
    return DocPlant(
        "drawing_note_conflict",
        [change],
        _expected("drawing_note_conflict", quantity=float(n2 - n)),
        Location(blocks=[new.id, spec.id]),
        Location(blocks=[spec.id]),
        {spec.id, new.id},
    )


def _try(kind: PlantKind, prep: Prep, rng: Random, used: set[str]) -> DocPlant | None:
    if kind == "contradicting_quantity":
        return contradicting_quantity(prep, rng, used)
    if kind in ("vite_clause", "index_clause"):
        return clause(kind, prep, rng, used)
    if kind == "delete_section":
        return delete_section(prep, rng, used)
    return drawing_note_conflict(prep, rng, used)


def plant_documents(prep: Prep, rng: Random, n: int) -> list[DocPlant]:
    """Up to n document plants of different kinds, on disjoint blocks, applied to `prep` in place."""
    kinds: list[PlantKind] = [
        "contradicting_quantity",
        "vite_clause",
        "index_clause",
        "delete_section",
        "drawing_note_conflict",
    ]
    rng.shuffle(kinds)
    plants: list[DocPlant] = []
    used: set[str] = set()
    for kind in kinds:
        if len(plants) >= n:
            break
        plant = _try(kind, prep, rng, used)
        if plant is not None:
            plants.append(plant)
            used |= plant.used
    return plants
