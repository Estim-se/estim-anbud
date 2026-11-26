"""Code counts, groups and prices (#73 steps 5, 6 and 8). No model is called here.

**Object model.** Object records within the contract (included or unclear) are counted per object kind. List rows add
up (each cited row once); stated totals are compared with them. Totals stated for different locations add up; totals
without a location are whole-scope figures. When the candidates disagree, the lowest is used and a conflict note
says so, as #73 asks. An apartment row's rooms (`contains`, from the model, a table column or the row's own
descriptor) give room types per apartment (`rooms.room_types`), counted the same way with a breakdown per apartment
type. Room types no row counts (a hall) come from the recipe's per-apartment figure, labelled. A requirement per
building when no record counts the buildings multiplies by at least one, as the app does, labelled an assumption.

**Object list read by code.** When a sheet is an object list by the app's rule (`object_list`), that list is the
primary source for apartments, premises (when it has premises rows), stairwells and stairwell floors (when it has an
address and floor column) and the room types of its apartments' type labels, labelled "objektlistan läst av kod (som
appens objektmodell)". The model's list rows and stated totals for those kinds stay in the basis; a model figure
that differs is a conflict note (the list's count is used), and the blocks the model cited outside the list (rows it
counted, stated totals) are kept with the count. Every other kind (buildings, kitchens and bathrooms a row states) is
counted from the model's records as before. Stairwell floors are counted by code only: the model's object kinds don't
name them.

**Requirement quantities.** `stated` is the number written; `lump_sum` is 1; `missing` stays missing. `per_object` is
the number per object times the object count. A count the quotes don't write is no count, even when the model gives
one: then the recipe's count for that room type and catalog item (labelled with the recipe's source), else one per
object for a count unit (labelled "Antagande"), else "Saknar mängd" with the multiplier shown. A complete-installation
passage becomes one line per recipe item and room type, each citing the passage and the counted rows, unless a
requirement already states that item for that room type.

**Posts.** Requirement lines are grouped by scope, catalog item (or, without one, the normalised description), type
code, unit family and the object kind they multiply by. Inside a post, lines are grouped by location: the same
quantity stated twice counts once, a record marked additive adds, and different quantities for the same location are
a conflict priced at the lower value with a visible note. Locations add up. A line per object multiplies the whole
count of its kind, so its location never splits it: per-object lines are one group, and naming a room twice never
counts its objects twice. Quantities from per-space rules
(`DerivedQuantity`, common spaces) are posts of their own, labelled with the rule's source.

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
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from anbud.granskning.catalog import Catalog, CatalogItem
from anbud.granskning.contracts import CalcPost
from anbud.kalkyl.object_list import ObjectList, choose
from anbud.kalkyl.recipe import BoundRecipe, Count
from anbud.kalkyl.records import Contained, QuantityRecord, Scope, VerifiedRecord
from anbud.kalkyl.repetition import Repetition
from anbud.kalkyl.rooms import APARTMENT_ROOMS, room_types
from anbud.kalkyl.rooms import label as rooms_label
from anbud.kalkyl.verify import quantity_written
from anbud.models import Citation

OBJECT_LABELS = {
    "building": ("byggnad", "byggnader"),
    "stairwell": ("trapphus", "trapphus"),
    "stairwell_floor": ("trapphusplan", "trapphusplan"),
    "floor": ("våningsplan", "våningsplan"),
    "apartment": ("lägenhet", "lägenheter"),
    "premises": ("lokal", "lokaler"),
    "room": ("rum", "rum"),
    "living_room": ("vardagsrum", "vardagsrum"),
    "bedroom": ("sovrum", "sovrum"),
    "half_room": ("halvrum", "halvrum"),
    "hall": ("hall", "hallar"),
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
LEVEL_LABELS = {
    "exact": "Exakt citat",
    "visual": "Läst visuellt",
    "interpretation": "Tolkning",
    "recipe": "Enligt recept",
    "assumed": "Antagande",
}
# A per-object requirement in a count unit that names no number ("kitchens get a stove outlet") is one per object.
ASSUMED_PER_OBJECT = Decimal(1)
# Kinds a count the documents state comes first for, before one code derives from the object list's groups.
STATED_FIRST = ("building",)
DERIVED = "härledd"
# Kinds the app never counts as none: a requirement per building when no document states how many buildings
# multiplies by at least one (the app's "minst 1 (antal huskroppar anges inte)").
AT_LEAST_ONE = ("building",)
ASSUMED_COUNT_NOTE = (
    "Antal {many} antaget till minst 1: underlaget anger inget antal. Ersätts av ett antal i förfrågningsunderlaget "
    "eller ett platsbesök"
)
ASSUMED_NOTE = (
    "Antal per objekt antaget till 1: underlaget anger inget antal. Ersätts av ett antal i förfrågningsunderlaget, "
    "kundens recept för rumstypen eller ett platsbesök"
)
_UNIT_FAMILIES = {
    "count": {"st", "styck", "stycken", "pcs", "lgh", "rum", "sats", "set", "pkt", "post"},
    "length": {"m", "meter", "lm", "löpmeter"},
    "area": {"m2", "m²", "kvm"},
    "volume": {"m3", "m³"},
    "time": {"h", "tim", "timmar", "timme"},
    "mass": {"kg"},
}
ORE = Decimal("0.01")
CODE_BASIS = "objektlistan läst av kod (som appens objektmodell)"


def unit_family(unit: str) -> str:
    u = re.sub(r"[\s.]", "", unit.lower())
    for family, units in _UNIT_FAMILIES.items():
        if u in units:
            return family
    return f"other:{u}"


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").casefold()).strip(" .,:;")


def _d(value: float | int) -> Decimal:
    """A model or table number as a Decimal; whole numbers without a trailing .0."""
    d = Decimal(str(value))
    return d.quantize(Decimal(1)) if d == d.to_integral_value() else d


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
    rows: list[str] = field(default_factory=list)  # the cited blocks counted: list rows, or the stated totals
    note: str | None = None  # how a derived count was reached, when it isn't plain rows or totals
    source: str = "model"  # "code" when the object list read by code gave the count, "assumed" for `AT_LEAST_ONE`
    model_rows: list[str] = field(default_factory=list)  # the model's cited blocks outside the code-read list


def _held(r: QuantityRecord, n: Decimal) -> tuple[list[tuple[str, Decimal]], bool]:
    """What one counted object holds, times how many it is, and False when an apartment row's rooms are implausible.
    An apartment's rooms also give its room types (living room, bedrooms, half room) per `rooms.room_types`."""
    if r.object_kind != "apartment":
        return [(c.object_kind, n * _d(c.quantity)) for c in r.contains], True
    types = room_types(list(r.contains))
    if r.contains and not types:
        return [], False
    return [(kind, n * per) for kind, per in types.items()], True


@dataclass
class _Evidence:
    """What the model's verified object records say about one kind: candidate counts with why, and their sources."""

    candidates: list[tuple[Decimal, str]] = field(default_factory=list)
    record_ids: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    by_type: dict[str, list[Decimal]] = field(default_factory=dict)  # row label -> per apartment


def _model_evidence(records: list[VerifiedRecord]) -> tuple[dict[str, _Evidence], int]:
    """Per kind, the model's list rows (each cited row once) and stated totals; and how many apartment rows had
    implausible rooms."""
    rows: dict[str, dict[str, Decimal]] = defaultdict(dict)  # kind -> first cited block -> count
    totals: dict[str, dict[str, set[Decimal]]] = defaultdict(lambda: defaultdict(set))  # kind -> location -> values
    ids: dict[str, list[str]] = defaultdict(list)
    blocks: dict[str, list[str]] = defaultdict(list)
    by_type: dict[str, dict[str, list[Decimal]]] = defaultdict(lambda: defaultdict(list))  # kind -> row label -> per
    implausible = 0
    for v in records:
        r = v.record
        if r.record_kind != "object" or r.object_kind is None or r.scope not in ("included", "unclear"):
            continue
        if r.quantity_source == "list_row":
            n = _d(r.quantity if r.quantity is not None else 1)
        elif r.quantity_source == "stated" and r.quantity is not None:
            n = _d(r.quantity)
        else:
            continue
        held, plausible = _held(r, n)
        implausible += not plausible
        # The object itself, then what each one holds (an apartment row's kitchens and rooms), counted the same way.
        for kind, count in [(r.object_kind, n), *held]:
            if r.quantity_source == "list_row":
                rows[kind].setdefault(v.block_ids[0], count)
                if kind != r.object_kind and r.object_kind == "apartment":
                    by_type[kind][rooms_label(list(r.contains))].append(count)
            else:
                totals[kind][_norm(r.location)].add(count)
            ids[kind].append(v.id)
            blocks[kind].extend(b for b in v.block_ids if b not in blocks[kind])
    out: dict[str, _Evidence] = {}
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
        out[kind] = _Evidence(candidates, ids[kind], blocks[kind], dict(by_type.get(kind, {})))
    return out, implausible


def _per_type(by_type: dict[str, list[Decimal]]) -> str:
    if not by_type:
        return ""
    parts = [f"{lbl}: {len(vals)} lgh × {_num(vals[0])}" for lbl, vals in sorted(by_type.items())]
    return " – per lägenhetstyp: " + "; ".join(parts)


def _from_model(kind: str, ev: _Evidence, derived: _Code | None = None) -> ObjectCount:
    """The model's evidence: when the candidates disagree, the lowest is used and the conflict says so. A count the
    code derived for a `STATED_FIRST` kind goes into the basis, and a conflict when it differs."""
    values = sorted({c for c, _ in ev.candidates})
    basis = "; ".join(f"{_num(c)} ({why})" for c, why in ev.candidates) + _per_type(ev.by_type)
    conflict = f"Konflikt: {' / '.join(_num(v) for v in values)} – lägsta används" if len(values) > 1 else None
    if derived is not None:
        basis += f"; objektlistan: {_num(derived.count)} ({derived.how})"
        if derived.count != values[0]:
            conflict = (
                f"Konflikt: {_num(values[0])} enligt underlaget / {_num(derived.count)} {DERIVED} – underlaget används"
            )
    return ObjectCount(kind, values[0], basis, conflict, ev.record_ids, ev.blocks)


@dataclass
class _Code:
    count: Decimal
    rows: list[str]
    how: str = ""
    by_type: dict[str, list[Decimal]] = field(default_factory=dict)


def _code_counts(lst: ObjectList) -> tuple[dict[str, _Code], int]:
    """Kind -> what the code-read list counts: apartments, premises (when the list has any), stairwells and stairwell
    floors (when it has an address and floor column), and room types from each apartment's type label. Also how
    many apartments had implausible rooms (more than six bedrooms: no room types)."""
    out = {"apartment": _Code(Decimal(len(lst.apartments)), [a.block_id for a in lst.apartments])}
    if lst.premises_rows:
        out["premises"] = _Code(Decimal(lst.premises), list(lst.premises_rows))
    stairwells = lst.stairwells()
    if stairwells:
        rows = list(dict.fromkeys(b for bs in stairwells.values() for b in bs))
        out["stairwell"] = _Code(Decimal(len(stairwells)), rows, f"unika adresser i kolumn {lst.address_column}")
    buildings = lst.buildings()
    if buildings:
        rows = list(dict.fromkeys(b for bs in buildings.values() for b in bs))
        out["building"] = _Code(
            Decimal(len(buildings)), rows, f"{DERIVED}: unika värden i kolumn {lst.building_column}"
        )
    floors = lst.stairwell_floors()
    if floors:
        rows = list(dict.fromkeys(b for bs in floors.values() for b in bs))
        how = f"unika par av adress och plan i kolumn {lst.address_column} och {lst.floor_column}"
        out["stairwell_floor"] = _Code(Decimal(len(floors)), rows, how)
    implausible = 0
    for a in lst.apartments:
        rooms = [Contained(object_kind="room", quantity=a.rooms)]
        types = room_types(rooms)
        if not types:
            implausible += 1
            continue
        for kind, per in types.items():
            code = out.setdefault(kind, _Code(Decimal(0), [], f"rum per lägenhet ur typkolumn {lst.type_column}"))
            code.count += per
            code.rows.append(a.block_id)
            code.by_type.setdefault(rooms_label(rooms), []).append(per)
    return out, implausible


def _derived_note(kind: str, code: _Code) -> str | None:
    if not code.how.startswith(DERIVED):
        return None
    many = OBJECT_LABELS[kind][1]
    return f"{DERIVED.capitalize()}: antal {many} anges inte i underlaget, objektlistan grupperar raderna efter {many}"


def _from_code(kind: str, code: _Code, ev: _Evidence | None, lst: ObjectList, found: int) -> ObjectCount:
    """The code-read list's count is used. The model's list rows and stated totals stay in the basis and its records
    with the count; a model figure that differs is a conflict, and the blocks it cited outside the list are kept."""
    where = f"{CODE_BASIS}: blad {lst.sheet}, typkolumn {lst.type_column}, rad {lst.first_row}–{lst.last_row}"
    if found > 1:
        where += f"; {found} listor hittades, den med flest lägenheter används som i appen, listor adderas inte"
    if code.how:
        where += f"; {code.how}"
    basis = f"{_num(code.count)} ({where}){_per_type(code.by_type)}"
    if ev is None:
        return ObjectCount(kind, code.count, basis, None, [], code.rows, note=_derived_note(kind, code), source="code")
    basis += "; modellen: " + "; ".join(f"{_num(c)} ({why})" for c, why in ev.candidates) + _per_type(ev.by_type)
    differ = sorted({c for c, _ in ev.candidates if c != code.count})
    conflict = None
    if differ:
        model = " / ".join(_num(v) for v in differ)
        conflict = f"Konflikt: {_num(code.count)} enligt objektlistan / {model} enligt modellen – objektlistan används"
    listed = set(code.rows)
    outside = [b for b in ev.blocks if b not in listed]
    return ObjectCount(kind, code.count, basis, conflict, ev.record_ids, code.rows, source="code", model_rows=outside)


def count_objects(
    records: list[VerifiedRecord], recipe: BoundRecipe | None = None, lists: list[ObjectList] | None = None
) -> dict[str, ObjectCount]:
    """Object counts per kind. With object lists read by code (`object_list`), the chosen list counts apartments,
    premises, stairwells, stairwell floors and room types; the model's evidence counts every other kind, and every
    kind when there is no list."""
    evidence, implausible = _model_evidence(records)
    chosen = choose(lists or [])
    code, code_implausible = _code_counts(chosen) if chosen else ({}, 0)
    out: dict[str, ObjectCount] = {}
    for kind in sorted(set(evidence) | set(code)):
        if kind in STATED_FIRST and kind in evidence:
            out[kind] = _from_model(kind, evidence[kind], code.get(kind))
        elif chosen is not None and kind in code:
            out[kind] = _from_code(kind, code[kind], evidence.get(kind), chosen, len(lists or []))
        else:
            out[kind] = _from_model(kind, evidence[kind])
    _apartment_notes(out, code_implausible if "room" in code else implausible)
    _per_apartment(out, recipe)
    _at_least_one(out, records)
    return out


def _at_least_one(out: dict[str, ObjectCount], records: list[VerifiedRecord]) -> None:
    """A kind in `AT_LEAST_ONE` that a requirement multiplies by and no record counts: at least one, labelled an
    assumption, so the requirement is priced with its assumption visible instead of missing."""
    needed = {v.record.per_object for v in records if v.record.quantity_source == "per_object"}
    for kind in AT_LEAST_ONE:
        if kind in needed and (kind not in out or out[kind].count is None):
            many = OBJECT_LABELS[kind][1]
            out[kind] = ObjectCount(
                kind,
                Decimal(1),
                f"minst 1 (antal {many} anges inte)",
                note=ASSUMED_COUNT_NOTE.format(many=many),
                source="assumed",
            )


def _apartment_notes(out: dict[str, ObjectCount], implausible: int) -> None:
    """Room types derived from fewer rows than the apartment count say so, and so do rows left out as implausible."""
    apartments = out.get("apartment")
    if apartments is None or apartments.count is None:
        return
    described = len(out["room"].rows) if "room" in out else 0
    for kind in ("room", "living_room", "bedroom", "half_room"):
        obj = out.get(kind)
        if obj is None:
            continue
        notes = []
        if described != apartments.count:
            notes.append(f"rumstyper från {described} lägenhetsrader med rumsuppgift av {_num(apartments.count)}")
        if implausible:
            notes.append(f"{implausible} rader med orimligt rumsantal utelämnade")
        obj.note = "; ".join(notes) or None


def _per_apartment(out: dict[str, ObjectCount], recipe: BoundRecipe | None) -> None:
    """Room types every apartment has one of (a hall, a kitchen), from the recipe, when no row counts them."""
    apartments = out.get("apartment")
    if recipe is None or apartments is None or apartments.count is None:
        return
    for kind, per in recipe.per_apartment.items():
        if kind in out:
            continue
        out[kind] = ObjectCount(
            kind,
            per * apartments.count,
            f"{_num(per)} per lägenhet ({recipe.label}) × {_num(apartments.count)} lägenheter",
            apartments.conflict,
            list(apartments.record_ids),
            list(apartments.rows),
            note=f"{OBJECT_LABELS[kind][1]} per lägenhet enligt {recipe.label}",
        )


@dataclass
class Line:
    """One contribution to a post: a requirement record, or a recipe count the record's scope passage calls for."""

    record: VerifiedRecord
    quantity: Decimal | None
    basis: str
    note: str | None = None
    per: str | None = None  # the object kind or room type multiplied by
    objects: ObjectCount | None = None  # the counted objects, for their rows
    recipe: str | None = None  # the recipe label, when the count per object comes from a recipe
    name: str | None = None  # the post name, when it isn't the record's
    catalog_id: str | None = None  # the item, when the line is a recipe count
    unit: str | None = None
    assumed: bool = False  # the count per object is ASSUMED_PER_OBJECT: no number written, no recipe count


def line_of(
    v: VerifiedRecord, objects: dict[str, ObjectCount], recipe: BoundRecipe | None = None, catalog_id: str | None = None
) -> Line:
    r = v.record
    unit = r.unit
    src = r.quantity_source
    if src in ("stated", "list_row"):
        q = _d(r.quantity if r.quantity is not None else 1)
        return Line(v, q, f"angivet {_num(q)} {unit}")
    if src == "lump_sum":
        return Line(v, Decimal(1), "klumpsumma, 1 post")
    if src == "per_object" and r.per_object is not None:
        kind = r.per_object
        one, many = OBJECT_LABELS[kind]
        obj = objects.get(kind)
        # A count the quotes don't write (the model's own "1", or none) is no stated count: the recipe's count for
        # that room type and item comes first, then one per object for a count unit.
        stated = _d(r.quantity) if r.quantity is not None and quantity_written(r) else None
        hit = recipe.count_for(kind, catalog_id) if recipe and stated is None else None
        # A complete installation is no single item: without a recipe it stays missing, never "1 per apartment".
        assumed = stated is None and hit is None and unit_family(unit) == "count" and not r.complete_installation
        per = stated if stated is not None else hit.count if hit else ASSUMED_PER_OBJECT if assumed else None
        label = hit.label if hit else None
        each = f"? per {one}" if per is None else f"{_num(per)} per {one}"
        each += f" ({label})" if label else " (antaget)" if assumed else ""
        if obj is None or obj.count is None:
            return Line(v, None, each, f"antal {many} saknas i objektmodellen", per=kind, assumed=assumed)
        times = f"{each} × {_num(obj.count)} {many}"
        if per is None:
            return Line(v, None, times, f"Saknar mängd: ange antal per {one}", per=kind, objects=obj)
        return Line(v, per * obj.count, times, obj.conflict, per=kind, objects=obj, recipe=label, assumed=assumed)
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
    mapped_by: str | None = None  # R2, R3, recipe, or None
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
    hours_flat: Decimal | None = None  # quantity × hours per unit, before the repetition factor
    repetition_factor: Decimal | None = None  # the average factor on hours, when below 1

    @property
    def installed_unit_price(self) -> Decimal | None:
        """Material and labour per unit after repetition: "installerat pris per enhet", to compare line by line."""
        if self.total is None or not self.quantity:
            return None
        return _round(self.total / self.quantity)

    @property
    def assumed_per_object(self) -> bool:
        """Some of the quantity rests on an assumed count per object."""
        return any(ln.assumed for ln in self.lines)

    @property
    def assumed_objects(self) -> bool:
        """Some of the quantity multiplies by an assumed object count (`AT_LEAST_ONE`)."""
        return any(ln.objects is not None and ln.objects.source == "assumed" for ln in self.lines)

    @property
    def assumed(self) -> bool:
        """Some of the quantity rests on an assumption: a count per object or an object count."""
        return self.assumed_per_object or self.assumed_objects

    @property
    def assumed_lines(self) -> list[Line]:
        """The lines whose count rests on an assumption, for their citations and bases."""
        return [ln for ln in self.lines if ln.assumed or (ln.objects is not None and ln.objects.source == "assumed")]

    @property
    def level(self) -> str:
        if self.assumed:
            return LEVEL_LABELS["assumed"]
        if any(ln.recipe for ln in self.lines):
            return LEVEL_LABELS["recipe"]
        levels = {ln.record.level for ln in self.lines}
        for key in ("visual", "interpretation"):
            if key in levels:
                return LEVEL_LABELS[key]
        return LEVEL_LABELS["exact"]

    @property
    def basis(self) -> str:
        return "; ".join(dict.fromkeys(ln.basis for ln in self.lines))

    @property
    def rows(self) -> list[str]:
        """The object rows the quantity counts (list rows or stated totals), each once."""
        return list(dict.fromkeys(b for ln in self.lines if ln.objects for b in ln.objects.rows))

    @property
    def priced_in_total(self) -> bool:
        return self.status == "Prissatt" and self.scope in ("included", "unclear")


def _post_quantity(lines: list[Line]) -> tuple[Decimal | None, list[str]]:
    by_loc: dict[str, list[Line]] = defaultdict(list)
    for ln in lines:
        # A line per object multiplies the whole count of its kind, so a location never splits it: every such line
        # covers the same objects, and adding two of them would count the objects twice.
        by_loc["" if ln.per is not None else _norm(ln.record.record.location)].append(ln)
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
        extra_notes = [ln.note, f"Antal per objekt enligt {ln.recipe}" if ln.recipe else None]
        extra_notes.append(ASSUMED_NOTE if ln.assumed else None)
        extra_notes.append(ln.objects.note if ln.objects else None)
        for note in extra_notes:
            if note and note not in notes:
                notes.append(note)
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


@dataclass(frozen=True)
class DerivedQuantity:
    """A quantity a per-space rule derived outside R2 (common spaces from the space list, builder A's rules on
    `granskning-checks`): Kalkyl groups and prices it like any post. `citations` are the passage that sets the scope
    and the rows counted; `label` names the rule's source and is shown on the post."""

    name: str
    unit: str
    scope: Scope
    quantity: Decimal | None
    basis: str
    label: str
    citations: tuple[Citation, ...]
    catalog_id: str | None = None


# Per-space rules plug in here: given the verified records and the object model, return derived quantities.
CommonSpaceRules = Callable[[list[VerifiedRecord], dict[str, ObjectCount]], list[DerivedQuantity]]


def _derived_line(n: int, d: DerivedQuantity) -> Line:
    record = QuantityRecord(
        record_kind="requirement",
        what=d.name[:200],
        quantity=float(d.quantity) if d.quantity is not None else None,
        quantity_source="stated" if d.quantity is not None else "missing",
        unit=d.unit[:20],
        scope=d.scope,
        catalog_id=d.catalog_id,
        citations=list(d.citations[:3]),
    )
    ids = list(dict.fromkeys(c.block_id for c in d.citations))
    v = VerifiedRecord(id=f"regel-{n:03d}", chunk_id="regler", record=record, level="interpretation", block_ids=ids)
    return Line(v, d.quantity, d.basis, f"Enligt {d.label}", name=d.name, catalog_id=d.catalog_id, unit=d.unit)


def _apartment_rooms(kind: str) -> set[str]:
    return {kind, *APARTMENT_ROOMS} if kind == "apartment" else {kind}


def _covered(c: Count, explicit: set[tuple[str, str]]) -> bool:
    return (c.room, c.item.id) in explicit or ("*", c.item.id) in explicit


def _recipe_lines(
    v: VerifiedRecord, objects: dict[str, ObjectCount], recipe: BoundRecipe | None, explicit: set[tuple[str, str]]
) -> list[Line]:
    """A complete-installation passage, room by room from the recipe: one line per recipe item and room type with
    objects, unless a requirement already states that item for that room type (or for the whole contract)."""
    kind = v.record.per_object or "apartment"
    out = []
    for c in recipe.for_rooms(_apartment_rooms(kind)) if recipe else []:
        if _covered(c, explicit):
            continue
        obj = objects.get(c.room)
        if obj is None or not obj.count:
            continue
        one, many = OBJECT_LABELS[c.room]
        basis = f"{_num(c.count)} per {one} ({c.label}) × {_num(obj.count)} {many}"
        name = f"{c.item.name}, {many}"
        out.append(
            Line(v, c.count * obj.count, basis, obj.conflict, c.room, obj, c.label, name, c.item.id, c.item.unit)
        )
    return out


def build_posts(
    records: list[VerifiedRecord],
    objects: dict[str, ObjectCount],
    catalog: Catalog,
    recipe: BoundRecipe | None = None,
    derived: list[DerivedQuantity] | None = None,
) -> list[Post]:
    """Group requirement records into posts. R2's catalog proposal is checked per record first; a proposal that fails
    is dropped with its reason, so the record groups by its description and the post goes to R3 or stays unmapped.
    Per-object lines group by the object kind they multiply by too, so sockets per bedroom and per living room are two
    posts. A complete-installation passage becomes the recipe's posts room by room."""
    groups: dict[tuple[str, ...], list[Line]] = defaultdict(list)
    dropped: dict[tuple[str, ...], str] = {}
    requirements = [v for v in records if v.record.record_kind == "requirement"]
    explicit: set[tuple[str, str]] = set()
    for v in requirements:
        r = v.record
        if not r.complete_installation and r.catalog_id and r.scope in ("included", "unclear"):
            if _usable(catalog, r.catalog_id, r.unit) is None:
                explicit.add((r.per_object or "*", r.catalog_id))
    for v in requirements:
        r = v.record
        if r.complete_installation:
            made = _recipe_lines(v, objects, recipe, explicit)
            for ln in made:
                unit = ln.unit or "st"
                groups[(r.scope, ln.catalog_id or "", "", "", unit_family(unit), ln.per or "")].append(ln)
            wanted = recipe.for_rooms(_apartment_rooms(r.per_object or "apartment")) if recipe else []
            if made or (wanted and all(_covered(c, explicit) for c in wanted)):
                continue  # covered room by room, by the recipe or by stated requirements
            # No recipe, or no counted rooms for it: one post missing its quantity, with the multiplier shown.
        problem = _usable(catalog, r.catalog_id, r.unit)
        cid = None if problem else r.catalog_id
        per = r.per_object if r.quantity_source == "per_object" else None
        key = (r.scope, cid or "", "" if cid else _norm(r.what), _norm(r.type_code), unit_family(r.unit), per or "")
        groups[key].append(line_of(v, objects, recipe, cid))
        if problem:
            dropped.setdefault(key, f"förslag avvisat: {problem}")
    for n, d in enumerate(derived or [], 1):
        name = "" if d.catalog_id else _norm(d.name)
        groups[(d.scope, d.catalog_id or "", name, "", unit_family(d.unit), d.label)].append(_derived_line(n, d))
    posts = []
    for key, lines in groups.items():
        first = lines[0]
        quantity, notes = _post_quantity(lines)
        cid = key[1] or None
        by_recipe = all(ln.recipe for ln in lines)
        posts.append(
            Post(
                id=_post_id(key),
                name=first.name or first.record.record.what,
                scope=first.record.record.scope,
                unit=first.unit or first.record.record.unit,
                type_code=first.record.record.type_code if not first.name else None,
                catalog_id=cid,
                lines=lines,
                quantity=quantity,
                notes=notes,
                mapped_by=("recept" if by_recipe else "R2") if cid else None,
                mapping_note=None if cid else dropped.get(key, "ingen katalogpost föreslogs"),
            )
        )
    order = {"included": 0, "unclear": 1, "option": 2, "other_trade": 3, "excluded": 4}
    posts.sort(key=lambda p: (order[p.scope], p.lines[0].record.id, p.name))
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


def price_posts(posts: list[Post], catalog: Catalog, repetition: Repetition | None = None) -> None:
    """Priced as `granskning.catalog.Catalog.unit_cost` defines a unit (hours x rate + material), in Decimal. A missing
    unit price counts as 0 when hours are given, and the other way round; hours without a rate can't be priced. With a
    repetition table, a count post's hours take its average factor (`repetition.Repetition.hours_factor`), noted."""
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
        post.hours_flat = _round(post.quantity * hours)
        post.hours = post.hours_flat
        if repetition is not None and unit_family(post.unit) == "count" and hours > 0:
            factor = repetition.hours_factor(post.quantity)
            if factor < 1:
                post.repetition_factor = factor.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
                post.hours = _round(post.quantity * hours * factor)
                post.notes.append(
                    f"Serieeffekt: timmar × {_num(post.repetition_factor)} i snitt för {_num(post.quantity)} "
                    f"{post.unit} ({repetition.label})"
                )
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
    assumed_total: Decimal = Decimal(0)  # the part of `total` resting on an assumed count per object
    repetition_hours: Decimal = Decimal(0)  # hours the repetition factor took off, in `total`'s posts
    priced: int = 0
    assumed_priced: int = 0
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
        if p.assumed:
            t.assumed_total += p.total
            t.assumed_priced += 1
        if p.hours_flat is not None and p.hours is not None:
            t.repetition_hours += p.hours_flat - p.hours
    return t


def assumed_posts(posts: list[Post]) -> list[Post]:
    """The posts in `Totals.assumed_total`, largest first: the ones an estimator checks first."""
    out = [p for p in posts if p.assumed and p.status == "Prissatt" and p.total is not None and p.scope != "option"]
    return sorted(out, key=lambda p: (-(p.total or 0), p.id))


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
