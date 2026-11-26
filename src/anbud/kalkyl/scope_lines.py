"""Scope lines: descriptive passages that set what is installed per object or per room, with no number and unit next
to it. The quantity-line test in `lines` never sees them, so the scope pass finds them by form and structure.

A scope line is a paragraph, a page-card line or a table row without a quantity that names an electrical item or an
installation verb, and an object kind or room type, in its own text or in the heading it sits under. The words are
generic Swedish building and electrical vocabulary (object kinds, room types, installation items and verbs), the same
kind of list as the unit tokens in `lines`; there are no phrases from any tender.

Each object or room word maps to the `ObjectKind` enum, so code can say which kind a passage names without reading
anything else.
"""

from __future__ import annotations

import re

from anbud.kalkyl.records import ObjectKind
from anbud.models import Block

_W = r"(?<![^\W\d_])"  # no letter before
_E = r"(?![^\W\d_])"  # no letter after

# Object kinds and room types, inflected and genitive forms included. Common spaces (storage, laundry, bike rooms) are
# left to the per-space rules, so they aren't here.
OBJECT_WORDS: dict[ObjectKind, re.Pattern[str]] = {
    kind: re.compile(rf"{_W}(?:{words})s?{_E}", re.IGNORECASE)  # a genitive s too
    for kind, words in (
        ("apartment", r"lägenhet(?:en|er|erna)?|lgh|bostad(?:en)?|bostäder(?:na)?"),
        ("bedroom", r"sovrum(?:met|men)?"),
        ("living_room", r"vardagsrum(?:met|men)?|allrum(?:met|men)?"),
        ("half_room", r"halvrum(?:met|men)?"),
        ("hall", r"hall(?:en|ar|arna)?"),
        ("kitchen", r"kök(?:et|en)?|kokvrå(?:n|er)?"),
        ("bathroom", r"badrum(?:met|men)?|duschrum(?:met|men)?|våtrum(?:met|men)?"),
        ("wc", r"wc|toalett(?:en|er|erna)?"),
        ("room", r"rum(?:met|men)?"),
        ("premises", r"lokal(?:en|er|erna)?"),
        ("stairwell", r"trapphus(?:et|en)?|trappuppgång(?:en|ar|arna)?"),
    )
}
# Electrical items, matched anywhere in a word so compounds count (a wall socket, a flat's consumer unit).
_ITEMS = re.compile(
    r"uttag|armatur|belysning|ljuspunkt|brytare|strömställare|dimmer|dos(?:a|or)|central(?:en|er)?(?![^\W\d_])|"
    r"mätare|brandvarnare|rökdetektor|rökvarnare|laddbox|laddpunkt|laddstation|porttelefon|golvvärme|handdukstork|"
    r"spegelskåp|elinstallation|elanslutning|kanalisation|spisvakt|bredband|fiber|antenn",
    re.IGNORECASE,
)
# Installation verbs, at the start of a word.
_VERBS = re.compile(rf"{_W}(?:installer|elinstaller|monter|förse|försedd|anslut|förlägg|placer|utrust)", re.IGNORECASE)
_KINDS = ("paragraph", "tile_text", "table_row")


def object_kinds_in(text: str) -> list[ObjectKind]:
    """The object kinds and room types a text names, in enum order."""
    return [kind for kind, pattern in OBJECT_WORDS.items() if pattern.search(text)]


def names_installation(text: str) -> bool:
    return bool(_ITEMS.search(text) or _VERBS.search(text))


def is_scope_line(block: Block, heading: str, quantity: bool) -> bool:
    """A passage that sets per-object scope: not a heading, not a quantity line (those are handled as such), naming an
    item or installation verb, and an object kind or room type in its text or its nearest heading."""
    if quantity or block.kind not in _KINDS:
        return False
    text = block.text.strip()
    if not text or not names_installation(text):
        return False
    return bool(object_kinds_in(text) or object_kinds_in(heading))
