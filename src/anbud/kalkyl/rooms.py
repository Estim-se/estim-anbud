"""Room composition of apartment rows: code reads it from the row, and derives room counts per room type.

- **Descriptors.** A Swedish apartment list writes the rooms as "3 rok", "3 r.o.k", "3 rum och kök", "3 rum", "3,5
  rok" or "3½ rum", or as a word ("trea"). "rok" and "rum och kök" are that many rooms plus a kitchen; "rum" alone is
  rooms only. A row's descriptor is read from the cited row itself, so the model never has to write it out. A row with
  two different descriptors is left alone.
- **Room types.** Rooms in a descriptor exclude the kitchen and include the living room, so N rooms are one living
  room and N - 1 bedrooms, plus a half room when N ends in a half. A count the row states for a room type (a bedroom
  column the model named) wins over the derivation. More than six bedrooms means a wrong column, and that row adds no
  room types.

Nothing here reads words of any tender; the patterns are the generic notation of Swedish apartment lists.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal

from anbud.kalkyl.records import Contained, ObjectKind

MAX_BEDROOMS = 6
MAX_ROOMS = 12
_NUMBER = r"(\d{1,2}(?:[.,]5)?|\d{1,2}\s*½)"
_ROK = re.compile(
    rf"(?<![\d.,]){_NUMBER}\s*(?:(?P<k>rok|r\.\s*o\.\s*k\.?|r\s*o\s*k|rum\s+och\s+kök|rum\s*\+\s*kök|r\s*\+\s*k)|rum)"
    r"(?![^\W\d_])",
    re.IGNORECASE,
)
# The app's ROOM_WORDS: number words ("tre rum") and apartment nouns ("trea").
ROOM_WORDS = {
    "en": 1,
    "ett": 1,
    "etta": 1,
    "två": 2,
    "tvåa": 2,
    "tre": 3,
    "trea": 3,
    "fyra": 4,
    "fem": 5,
    "femma": 5,
    "sex": 6,
    "sexa": 6,
    "sju": 7,
    "sjua": 7,
}
_NOUNS = ("etta", "tvåa", "trea", "fyra", "femma", "sexa", "sjua")
_WORDS = {w: ROOM_WORDS[w] for w in _NOUNS}
_WORD = re.compile(rf"(?<![^\W\d_])({'|'.join(_WORDS)})(?![^\W\d_])", re.IGNORECASE)
# roomCountFromLabel's patterns. JS `\d` is ASCII and `\p{L}` a letter: `[0-9]` and `[^\W\d_]` here.
_LETTER = r"[^\W\d_]"
_APP_DIGITS = re.compile(rf"(?<![0-9.,])([0-9]+(?:[.,]5)?)\s*(?:rok|r\.?\s*o\.?\s*k|rum|r\s*\+\s*k)(?!{_LETTER})")
_APP_BARE = re.compile(r"^([0-9]+(?:[.,]5)?)(?:[ ,.]|\Z)")
_APP_NUMBER_WORD = re.compile(rf"(?<!{_LETTER})(en|ett|två|tre|fyra|fem|sex|sju)\s+(?:rum|rok)(?!{_LETTER})")
_APP_NOUN = re.compile(rf"(?<!{_LETTER})({'|'.join(_NOUNS)})(?!{_LETTER})")
APARTMENT_ROOMS: tuple[ObjectKind, ...] = (
    "room",
    "living_room",
    "bedroom",
    "half_room",
    "hall",
    "kitchen",
    "bathroom",
    "wc",
)


def _value(raw: str) -> float:
    raw = re.sub(r"\s+", "", raw).replace("½", ".5").replace(",", ".")
    return float(raw)


def room_count(label: str, bare: bool = True) -> float | None:
    """Rooms excluding the kitchen from one cell's type label ("3 rok", "4 rum och kök", "Typ B 2 r.o.k", "trea"), or
    None. A bare leading number counts only with `bare` (a dedicated room-count column). Half rooms are kept: "3,5
    rok" and "3½ rum" are 3.5. The app's `roomCountFromLabel`, ported as is."""
    s = re.sub(r"([0-9])\s*½", r"\1.5", re.sub(r"\s+", " ", label.lower())).strip()
    if not s:
        return None
    m = _APP_DIGITS.search(s) or (_APP_BARE.search(s) if bare else None)
    if m and float(m.group(1).replace(",", ".")) > 0:
        return float(m.group(1).replace(",", "."))
    word = _APP_NUMBER_WORD.search(s) or _APP_NOUN.search(s)
    return float(ROOM_WORDS[word.group(1)]) if word else None


def descriptor(text: str) -> list[Contained]:
    """The rooms (and kitchen) one row describes, or nothing when it has no descriptor or two different ones."""
    found: set[tuple[float, bool]] = set()
    for m in _ROK.finditer(text):
        rooms = _value(m.group(1))
        if 0 < rooms <= MAX_ROOMS:  # a larger number next to "rum" is an area or an id, not a room count
            found.add((rooms, m.group("k") is not None))
    if not found:
        for m in _WORD.finditer(text):
            found.add((float(_WORDS[m.group(1).lower()]), False))
    if len({rooms for rooms, _ in found}) != 1:
        return []
    rooms = next(iter(found))[0]
    out = [Contained(object_kind="room", quantity=rooms)]
    if any(kitchen for _, kitchen in found):
        out.append(Contained(object_kind="kitchen", quantity=1))
    return out


def with_descriptor(contains: list[Contained], row_text: str) -> list[Contained]:
    """`contains` plus what the row's descriptor adds for kinds it doesn't already name."""
    named = {c.object_kind for c in contains}
    if "room" in named and "kitchen" in named:
        return contains
    extra = [c for c in descriptor(row_text) if c.object_kind not in named]
    return [*contains, *extra] if extra else contains


def _dec(value: float) -> Decimal:
    d = Decimal(str(value))
    return d.quantize(Decimal(1)) if d == d.to_integral_value() else d


def room_types(contains: list[Contained]) -> dict[str, Decimal]:
    """Counts per room type for one apartment: what the row states, plus living room, bedrooms and half room derived
    from its rooms (only those it has). Empty when the bedroom count is implausible."""
    out = {c.object_kind: _dec(c.quantity) for c in contains}
    rooms = out.get("room")
    if rooms is None or rooms <= 0:
        return out
    whole = math.floor(rooms)
    derived = {"living_room": min(whole, 1), "bedroom": max(0, whole - 1), "half_room": int(rooms != whole)}
    for kind, n in derived.items():
        if n and kind not in out:
            out[kind] = Decimal(n)
    if out.get("bedroom", 0) > MAX_BEDROOMS:
        return {}
    return out


def label(contains: list[Contained]) -> str:
    """The row's room descriptor as a short Swedish label, for per-type breakdowns: `3 rum och kök`."""
    by = {c.object_kind: c.quantity for c in contains}
    rooms = by.get("room")
    if rooms is None:
        return "utan rumsuppgift"
    n = format(Decimal(str(rooms)).normalize(), "f").replace(".", ",")
    return f"{n} rum och kök" if by.get("kitchen") else f"{n} rum"
