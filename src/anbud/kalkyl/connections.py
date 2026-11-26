"""Connections to powered equipment another party supplies, and the demolition question. No model is called here.

General electrical estimating, from no tender: equipment can belong to another trade or the client (appliances,
pumps, electric heating, fans and air handling units, door and gate automation, lifts, a heating substation), but its
power feed and its connection are normally this contract's. So when the model sets a line aside as another trade's
work (`other_trade`) and the line names powered equipment, the equipment stays out of this contract's supply and code
adds a connection: one record per kind of equipment the line names, citing the line. Its count is the number the line
writes, else one per room type or object kind the line names (assumed, so the post is "Antagande"), else missing, with
a question. Equipment the calc already has a record for (a record naming it, or an item named after it) gets none, so
nothing counts twice.

Demolition: removing the existing electrical installation is normally this contract's in a refurbishment, other
trades' demolition is not. A demolition line set aside as another trade's work without naming who does it is asked as
that question (`coverage`).

The equipment kinds are generic Swedish names of kinds of powered equipment, matched anywhere in a word so compounds
count; the names in the posts are Estim's, never a tender's words.
"""

from __future__ import annotations

import re

from anbud.granskning.catalog import Catalog
from anbud.granskning.corpus import Corpus
from anbud.kalkyl.extract import ChunkResult
from anbud.kalkyl.records import ObjectKind, QuantityRecord, VerifiedRecord
from anbud.kalkyl.scope_lines import object_kinds_in
from anbud.models import Citation

# Kinds of powered equipment: Estim's name for the connection, and the generic words for it (lower-cased text).
EQUIPMENT: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(words))
    for name, words in (
        (
            "spis och häll",
            r"spis(?!vakt|kåp|fläkt)|(?:induktions|kok|keramik|(?<![a-zåäö]))häll(?:en|ar|arna)?(?![a-zåäö])",
        ),
        ("ugn", r"(?<!vågs)ugn"),
        ("mikrovågsugn", r"mikrovågsugn"),
        ("diskmaskin", r"diskmaskin"),
        (
            "kyl och frys",
            r"kyl(?:skåp|frys|/frys|maskin)|(?<![a-zåäö])kyl(?![a-zåäö])|frys(?:skåp|box)|(?<![a-zåäö])frys",
        ),
        ("tvättmaskin", r"tvättmaskin"),
        ("torktumlare och torkskåp", r"torktumlare|torkskåp"),
        ("spiskåpa och köksfläkt", r"spiskåp|spisfläkt|köksfläkt|kolfilterfläkt"),
        ("pump", r"(?<!värme)pump"),
        ("golvvärme", r"golvvärme"),
        ("handdukstork", r"handdukstork"),
        ("värmekabel", r"värmekabel|frostskydd|snösmält"),
        ("elradiator", r"elradiator|elelement"),
        ("varmvattenberedare", r"varmvattenberedare|elpanna"),
        ("värmepump", r"värmepump"),
        ("undercentral", r"undercentral|fjärrvärmecentral"),
        ("fläkt och aggregat", r"(?<!spis)(?<!köks)(?<!kolfilter)fläkt|aggregat"),
        ("dörrautomatik", r"dörrautomatik|dörröppnare|dörrstängare|automatdörr|automatisk\s+dörr"),
        ("portautomatik", r"portautomatik|garageport|rullport|grindautomatik|motor(?:styrd|driven)\s+(?:port|grind)"),
        ("hiss", r"(?<![a-zåäö])hiss"),
    )
)
DEMOLITION = re.compile(
    r"rivning|(?<![a-zåäö])riv(?:s|es|a|er|ning)?(?![a-zåäö])|demonter|nedmonter|borttag|tas\s+bort"
)
# Room types first, then the objects that hold them, so "i varje kök i lägenheterna" is per kitchen.
_SPECIFIC_FIRST: tuple[ObjectKind, ...] = (
    "kitchen",
    "bathroom",
    "wc",
    "hall",
    "bedroom",
    "living_room",
    "half_room",
    "premises",
    "stairwell",
    "apartment",
    "building",
)
_COUNT = re.compile(r"(?<![\w.,])(\d+)\s*(?:st|styck|stycken)(?![a-zåäö])")
CONNECTION_NOTE = (
    "Anslutning tillagd av kod: modellen bedömde raden som annan entreprenörs leverans, men matning och anslutning "
    "av utrustningen brukar ingå i elentreprenaden. Kontrollera"
)
QUESTION = "Fråga: hur många ska anslutas? Raden anger inget antal och ingen rumstyp"


def equipment_in(text: str) -> list[str]:
    """Estim's names of the kinds of powered equipment a text names, in `EQUIPMENT` order."""
    low = text.lower()
    return [name for name, pattern in EQUIPMENT if pattern.search(low)]


def is_demolition(text: str) -> bool:
    return bool(DEMOLITION.search(text.lower()))


def demolition_line_ids(corpus: Corpus) -> set[str]:
    return {b for b in corpus.order if corpus.blocks[b].kind != "heading" and is_demolition(corpus.blocks[b].text)}


def _in_calc(name: str, verified: list[VerifiedRecord], catalog: Catalog) -> bool:
    pattern = dict(EQUIPMENT)[name]
    for v in verified:
        r = v.record
        if r.record_kind != "requirement" or r.scope not in ("included", "unclear", "option"):
            continue
        item = catalog.find(r.catalog_id) if r.catalog_id else None
        if pattern.search(r.what.lower()) or (item is not None and pattern.search(item.name.lower())):
            return True
    return False


def _record(name: str, block_id: str, text: str) -> QuantityRecord:
    cite = [Citation(block_id=block_id, quote=text)]
    base = {"record_kind": "requirement", "what": f"Anslutning {name}", "unit": "st", "scope": "included"}
    counts = {int(n) for n in _COUNT.findall(text.lower())}
    if len(counts) == 1:
        return QuantityRecord(**base, quantity_source="stated", quantity=counts.pop(), citations=cite)
    kinds = object_kinds_in(text)
    kind = next((k for k in _SPECIFIC_FIRST if k in kinds), None)
    if kind is not None:
        return QuantityRecord(**base, quantity_source="per_object", per_object=kind, citations=cite)
    return QuantityRecord(**base, quantity_source="missing", citations=cite)


def connection_records(
    results: list[ChunkResult], verified: list[VerifiedRecord], corpus: Corpus, catalog: Catalog
) -> list[VerifiedRecord]:
    """A connection record for each kind of powered equipment named by a line the model set aside as another trade's
    work and no record used, unless the calc already has that equipment. Read by code: level "interpretation"."""
    used = {b for v in verified for b in v.block_ids}
    out: list[VerifiedRecord] = []
    seen: set[str] = set()
    for res in results:
        for item in res.set_aside:
            if item.kind != "other_trade":
                continue
            for block_id in item.block_ids:
                if block_id in used or block_id in seen or block_id not in corpus.blocks:
                    continue
                seen.add(block_id)
                text = corpus.blocks[block_id].text
                for name in equipment_in(text):
                    if _in_calc(name, verified, catalog):
                        continue
                    record = _record(name, block_id, text)
                    out.append(
                        VerifiedRecord(
                            id=f"{res.chunk_id}-k{len(out) + 1:03d}",
                            chunk_id=res.chunk_id,
                            record=record,
                            level="interpretation",
                            block_ids=[block_id],
                        )
                    )
    return out


def note_connections(posts: list, connections: list[VerifiedRecord]) -> None:
    """A post made of connection records says code added it and why; one without a count asks how many."""
    ids = {v.id for v in connections}
    for post in posts:
        if not any(ln.record.id in ids for ln in post.lines):
            continue
        post.notes.append(CONNECTION_NOTE)
        if post.quantity is None:
            post.notes.append(QUESTION)
