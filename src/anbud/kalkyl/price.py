"""Code counts, groups and prices (#73 steps 5, 6 and 8). No model is called here.

**Object model.** Object records within the contract (included or unclear) are counted per object kind. List rows add
up (each cited row once); stated totals are compared with them. Totals stated for different locations add up; totals
without a location are whole-scope figures. When the candidates disagree, the lowest is used and a conflict note
says so, as #73 asks.

**Requirement quantities.** `stated` is the number written; `per_object` is the number per object times the object
count (missing when the object model has no count for that kind); `lump_sum` is 1; `missing` stays missing.

**Posts.** Requirement records are grouped by scope, catalog item (or, without one, the normalised description), type
code and unit family. Inside a post, records are grouped by location: the same quantity stated twice counts once, a
record marked additive adds, and different quantities for the same location are a conflict priced at the lower value
with a visible note. Locations add up.

**Catalog mapping.** A post's catalog item must exist and its unit must be in the same family as the post's (counts,
length, area, volume, hours). A post without a usable item is "Saknar pris", never priced on the nearest item.

**Kronor.** Decimal, from the shared catalog's figures: material = quantity × unit price; hours = quantity × hours
per unit; labour = hours × hourly rate; each rounded to öre (hours to hundredths). Options are priced outside the
total; excluded and other-trade posts are listed, never priced.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from anbud.granskning.catalog import Catalog, CatalogItem
from anbud.granskning.contracts import CalcPost
from anbud.kalkyl.records import VerifiedRecord

OBJECT_LABELS = {
    "building": ("byggnad", "byggnader"),
    "stairwell": ("trapphus", "trapphus"),
    "floor": ("våningsplan", "våningsplan"),
    "apartment": ("lägenhet", "lägenheter"),
    "premises": ("lokal", "lokaler"),
    "room": ("rum", "rum"),
    "kitchen": ("kök", "kök"),
    "bathroom": ("badrum", "badrum"),
    "wc": ("wc", "wc"),
    "other": ("övrigt objekt", "övriga objekt"),
}
SCOPE_LABELS = {
    "included": "Ingår",
    "unclear": "Oklar entreprenadgräns",
    "option": "Option",
    "excluded": "Ingår ej",
    "other_trade": "Utförs av annan",
}
LEVEL_LABELS = {"exact": "Exakt citat", "visual": "Läst visuellt", "interpretation": "Tolkning"}
_UNIT_FAMILIES = {
    "count": {"st", "styck", "stycken", "pcs", "lgh", "rum", "sats", "set", "pkt", "post"},
    "length": {"m", "meter", "lm", "löpmeter"},
    "area": {"m2", "m²", "kvm"},
    "volume": {"m3", "m³"},
    "time": {"h", "tim", "timmar", "timme"},
    "mass": {"kg"},
}
ORE = Decimal("0.01")


def unit_family(unit: str) -> str:
    u = re.sub(r"[\s.]", "", unit.lower())
    for family, units in _UNIT_FAMILIES.items():
        if u in units:
            return family
    return f"other:{u}"


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").casefold()).strip(" .,:;")


def _num(value: Decimal | float | None) -> str:
    if value is None:
        return "-"
    d = Decimal(str(value)).normalize()
    return format(d, "f").replace(".", ",")


@dataclass
class ObjectCount:
    kind: str
    count: Decimal | None
    basis: str
    conflict: str | None = None
    record_ids: list[str] = field(default_factory=list)


def count_objects(records: list[VerifiedRecord]) -> dict[str, ObjectCount]:
    rows: dict[str, dict[str, Decimal]] = defaultdict(dict)  # kind -> first cited block -> count
    totals: dict[str, dict[str, set[Decimal]]] = defaultdict(lambda: defaultdict(set))  # kind -> location -> values
    ids: dict[str, list[str]] = defaultdict(list)
    for v in records:
        r = v.record
        if r.record_kind != "object" or r.object_kind is None or r.scope not in ("included", "unclear"):
            continue
        if r.quantity_source == "list_row":
            n = Decimal(str(r.quantity if r.quantity is not None else 1))
        elif r.quantity_source == "stated" and r.quantity is not None:
            n = Decimal(str(r.quantity))
        else:
            continue
        # The object itself, then what each one holds (an apartment row's kitchens and rooms), counted the same way.
        for kind, count in [(r.object_kind, n), *((c.object_kind, n * Decimal(str(c.quantity))) for c in r.contains)]:
            if r.quantity_source == "list_row":
                rows[kind].setdefault(v.block_ids[0], count)
            else:
                totals[kind][_norm(r.location)].add(count)
            ids[kind].append(v.id)
    out: dict[str, ObjectCount] = {}
    for kind in sorted(set(rows) | set(totals)):
        candidates: list[tuple[Decimal, str]] = []
        if rows.get(kind):
            candidates.append((sum(rows[kind].values(), Decimal(0)), f"{len(rows[kind])} rader i lista"))
        located = {loc: vals for loc, vals in totals.get(kind, {}).items() if loc}
        if located:
            candidates.append(
                (sum(min(v) for v in located.values()), f"summa av angivna antal för {len(located)} delar")
            )
        for value in sorted(totals.get(kind, {}).get("", set())):
            candidates.append((value, "angivet totalt antal"))
        values = sorted({c for c, _ in candidates})
        chosen = values[0]
        basis = "; ".join(f"{_num(c)} ({why})" for c, why in candidates)
        conflict = None
        if len(values) > 1:
            conflict = f"Konflikt: {' / '.join(_num(v) for v in values)} – lägsta används"
        out[kind] = ObjectCount(kind, chosen, basis, conflict, ids[kind])
    return out


@dataclass
class Line:
    """One requirement record's contribution to a post."""

    record: VerifiedRecord
    quantity: Decimal | None
    basis: str
    note: str | None = None


def line_of(v: VerifiedRecord, objects: dict[str, ObjectCount]) -> Line:
    r = v.record
    unit = r.unit
    src = r.quantity_source
    if src in ("stated", "list_row"):
        q = Decimal(str(r.quantity if r.quantity is not None else 1))
        return Line(v, q, f"angivet {_num(q)} {unit}")
    if src == "lump_sum":
        return Line(v, Decimal(1), "klumpsumma, 1 post")
    if src == "per_object" and r.per_object is not None and r.quantity is not None:
        per = Decimal(str(r.quantity))
        one, many = OBJECT_LABELS[r.per_object]
        obj = objects.get(r.per_object)
        if obj is None or obj.count is None:
            return Line(v, None, f"{_num(per)} per {one}", f"antal {many} saknas i objektmodellen")
        return Line(v, per * obj.count, f"{_num(per)} per {one} × {_num(obj.count)} {many}", obj.conflict)
    return Line(v, None, "mängd saknas i underlaget")


@dataclass
class Post:
    id: str
    name: str
    scope: str
    unit: str
    type_code: str | None
    catalog_id: str | None
    lines: list[Line]
    quantity: Decimal | None = None
    notes: list[str] = field(default_factory=list)
    mapped_by: str | None = None  # R2, R3, or None
    mapping_note: str | None = None
    status: str = ""
    unit_price: Decimal | None = None
    hours_per_unit: Decimal | None = None
    material: Decimal | None = None
    hours: Decimal | None = None
    labour: Decimal | None = None
    total: Decimal | None = None
    category: str = ""
    catalog_name: str = ""

    @property
    def level(self) -> str:
        levels = {ln.record.level for ln in self.lines}
        for key in ("visual", "interpretation"):
            if key in levels:
                return LEVEL_LABELS[key]
        return LEVEL_LABELS["exact"]

    @property
    def basis(self) -> str:
        return "; ".join(dict.fromkeys(ln.basis for ln in self.lines))

    @property
    def priced_in_total(self) -> bool:
        return self.status == "Prissatt" and self.scope in ("included", "unclear")


def _post_quantity(lines: list[Line]) -> tuple[Decimal | None, list[str]]:
    by_loc: dict[str, list[Line]] = defaultdict(list)
    for ln in lines:
        by_loc[_norm(ln.record.record.location)].append(ln)
    total: Decimal | None = None
    notes: list[str] = []
    for loc, items in sorted(by_loc.items()):
        where = f" ({items[0].record.record.location})" if loc else ""
        base = sorted({ln.quantity for ln in items if not ln.record.record.additive and ln.quantity is not None})
        extra = [ln.quantity for ln in items if ln.record.record.additive and ln.quantity is not None]
        if any(ln.quantity is None for ln in items):
            notes.append(f"Mängd saknas för en del av posten{where}")
        if not base and not extra:
            continue
        q = base[0] if base else Decimal(0)
        if len(base) > 1:
            notes.append(f"Konflikt{where}: {' / '.join(_num(b) for b in base)} – lägsta används")
        q += sum(extra, Decimal(0))
        total = q if total is None else total + q
    for ln in lines:
        if ln.note and ln.note not in notes:
            notes.append(ln.note)
    return total, notes


def _post_id(key: tuple[str, ...]) -> str:
    return "k-" + hashlib.sha256("\x1f".join(key).encode()).hexdigest()[:10]


def _usable(catalog: Catalog, catalog_id: str | None, unit: str) -> str | None:
    """Why R2's proposed item can't be used for this unit, or None if it can (or none was proposed)."""
    if catalog_id is None:
        return None
    item = catalog.find(catalog_id) if catalog_id else None
    if item is None:
        return "okänd katalogpost"
    if unit_family(item.unit) != unit_family(unit):
        return f"enheten {unit} passar inte katalogpostens enhet {item.unit}"
    return None


def build_posts(records: list[VerifiedRecord], objects: dict[str, ObjectCount], catalog: Catalog) -> list[Post]:
    """Group requirement records into posts. R2's catalog proposal is checked per record first; a proposal that fails
    is dropped with its reason, so the record groups by its description and the post goes to R3 or stays unmapped."""
    groups: dict[tuple[str, ...], list[Line]] = defaultdict(list)
    dropped: dict[tuple[str, ...], str] = {}
    for v in records:
        r = v.record
        if r.record_kind != "requirement":
            continue
        problem = _usable(catalog, r.catalog_id, r.unit)
        cid = None if problem else r.catalog_id
        key = (r.scope, cid or "", "" if cid else _norm(r.what), _norm(r.type_code), unit_family(r.unit))
        groups[key].append(line_of(v, objects))
        if problem:
            dropped.setdefault(key, f"förslag avvisat: {problem}")
    posts = []
    for key, lines in groups.items():
        first = lines[0].record.record
        quantity, notes = _post_quantity(lines)
        cid = key[1] or None
        posts.append(
            Post(
                id=_post_id(key),
                name=first.what,
                scope=first.scope,
                unit=first.unit,
                type_code=first.type_code,
                catalog_id=cid,
                lines=lines,
                quantity=quantity,
                notes=notes,
                mapped_by="R2" if cid else None,
                mapping_note=None if cid else dropped.get(key, "ingen katalogpost föreslogs"),
            )
        )
    order = {"included": 0, "unclear": 1, "option": 2, "other_trade": 3, "excluded": 4}
    posts.sort(key=lambda p: (order[p.scope], p.lines[0].record.id))
    return posts


def mapping_problem(post: Post, catalog: Catalog, catalog_id: str | None) -> str | None:
    if not catalog_id:
        return "ingen katalogpost vald"
    return _usable(catalog, catalog_id, post.unit)


def _round(value: Decimal) -> Decimal:
    return value.quantize(ORE, rounding=ROUND_HALF_UP)


def _dec(value: float | None) -> Decimal:
    """A catalog figure (a float in the shared format) as the Decimal it was written as; a missing one is 0."""
    return Decimal(str(value)) if value is not None else Decimal(0)


def price_posts(posts: list[Post], catalog: Catalog) -> None:
    """Priced as `granskning.catalog.Catalog.unit_cost` defines a unit (hours x rate + material), in Decimal. A missing
    unit price counts as 0 when hours are given, and the other way round; hours without a rate can't be priced."""
    for post in posts:
        item: CatalogItem | None = catalog.find(post.catalog_id) if post.catalog_id else None
        post.category = (item.category or "") if item else ""
        post.catalog_name = item.name if item else ""
        if post.scope == "excluded":
            post.status = "Ingår ej"
            continue
        if post.scope == "other_trade":
            post.status = "Utförs av annan"
            continue
        if post.quantity is None:
            post.status = "Saknar mängd"
            continue
        if item is None:
            post.status = "Saknar pris"
            continue
        if item.unit_price is None and item.hours is None:
            post.status = "Saknar pris"
            post.notes.append("katalogposten saknar pris och tid")
            continue
        if item.hours and catalog.hourly_rate <= 0:
            post.status = "Saknar pris"
            post.notes.append("katalogen saknar timpris")
            continue
        unit_price, hours = _dec(item.unit_price), _dec(item.hours)
        post.unit_price, post.hours_per_unit = unit_price, hours
        post.material = _round(post.quantity * unit_price)
        post.hours = _round(post.quantity * hours)
        post.labour = _round(post.hours * _dec(catalog.hourly_rate))
        post.total = post.material + post.labour
        post.status = "Prissatt"


@dataclass
class Totals:
    material: Decimal = Decimal(0)
    hours: Decimal = Decimal(0)
    labour: Decimal = Decimal(0)
    total: Decimal = Decimal(0)
    unclear_total: Decimal = Decimal(0)
    options_total: Decimal = Decimal(0)
    priced: int = 0
    missing_price: int = 0
    missing_quantity: int = 0


def totals_of(posts: list[Post]) -> Totals:
    t = Totals()
    for p in posts:
        if p.status == "Saknar pris" and p.scope in ("included", "unclear", "option"):
            t.missing_price += 1
        if p.status == "Saknar mängd" and p.scope in ("included", "unclear", "option"):
            t.missing_quantity += 1
        if p.status != "Prissatt" or p.total is None:
            continue
        if p.scope == "option":
            t.options_total += p.total
            continue
        t.priced += 1
        t.material += p.material or 0
        t.hours += p.hours or 0
        t.labour += p.labour or 0
        t.total += p.total
        if p.scope == "unclear":
            t.unclear_total += p.total
    return t


def calc_posts(posts: list[Post], rows: dict[str, int]) -> list[CalcPost]:
    """The shared contract for Granskning and planted defects: every post of the calc, priced or not. Excluded and
    other-trade posts aren't calc lines and are left out."""
    out = []
    for p in posts:
        if p.scope in ("excluded", "other_trade"):
            continue
        section = SCOPE_LABELS["option"] if p.scope == "option" else (p.category or SCOPE_LABELS[p.scope])
        out.append(
            CalcPost(
                id=p.id,
                sheet="Kalkyl",
                row=rows.get(p.id),
                section=section,
                text=p.name if not p.type_code else f"{p.name} ({p.type_code})",
                quantity=float(p.quantity) if p.quantity is not None else None,
                unit=p.unit,
                unit_price=float(p.unit_price) if p.unit_price is not None else None,
                amount=float(p.total) if p.total is not None else None,
                hours=float(p.hours) if p.hours is not None else None,
            )
        )
    return out
