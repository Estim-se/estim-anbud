"""Text forms and stable block ids.

`clean` is the display form a block carries. `normalise` is the comparison form: block ids hash it, and citation checks
compare quotes in it, so a dash, quote or whitespace variant never breaks either.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter

_WS = re.compile(r"\s+")
# Code points, not literals, so the source shows what is removed and unified.
_SOFT_HYPHEN_AND_ZERO_WIDTH = (0x00AD, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF)
_DASHES = (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212)
_SINGLE_QUOTES = (0x2018, 0x2019, 0x201A, 0x201B)
_DOUBLE_QUOTES = (0x201C, 0x201D, 0x201E, 0x201F)
_INVISIBLE: dict[int, None] = dict.fromkeys(_SOFT_HYPHEN_AND_ZERO_WIDTH, None)
_UNIFY = dict.fromkeys(_DASHES, "-") | dict.fromkeys(_SINGLE_QUOTES, "'") | dict.fromkeys(_DOUBLE_QUOTES, '"')

DOC_PREFIX = 6
TEXT_PREFIX = 6


def clean(text: str) -> str:
    """NFC, invisible characters removed, whitespace collapsed. Keeps characters like ² that NFKC would rewrite."""
    return _WS.sub(" ", unicodedata.normalize("NFC", text).translate(_INVISIBLE)).strip()


def normalise(text: str) -> str:
    """NFKC, dashes and quotes unified, whitespace collapsed."""
    return _WS.sub(" ", unicodedata.normalize("NFKC", text.translate(_INVISIBLE)).translate(_UNIFY)).strip()


def text_hash(text: str) -> str:
    return hashlib.sha256(normalise(text).encode()).hexdigest()


def doc_id_of(sha256: str) -> str:
    return sha256[:12]


class BlockIds:
    """Ids for one document's blocks, in reading order: `<file hash>-<text hash>`, then `-2`, `-3` for repeats.

    The id depends only on the file's bytes, the block's text and how often that text came before it in the same
    file, so it survives re-preparation, other files changing and masking rules changing (ids hash the unmasked text).
    """

    def __init__(self, sha256: str) -> None:
        self.prefix = sha256[:DOC_PREFIX]
        self._seen: Counter[str] = Counter()

    def next(self, text: str) -> str:
        key = text_hash(text)[:TEXT_PREFIX]
        self._seen[key] += 1
        n = self._seen[key]
        return f"{self.prefix}-{key}" if n == 1 else f"{self.prefix}-{key}-{n}"


# Headings: multi-level numbers ("2.3.1 Title") and dotted codes ("ABC.12 Title", "12.AB/3 Title"). Only the shape
# is matched.
NUMBERED = re.compile(r"^(\d{1,2}(?:\.\d{1,3}){1,5})\.?\s+(?=\S)")
SINGLE_NUMBER = re.compile(r"^(\d{1,2})\.?\s+(?=[A-ZÅÄÖ])")
CODED = re.compile(r"^((?:[A-ZÅÄÖ]{1,5}|\d{2})\.[0-9A-ZÅÄÖ]{1,4}(?:[./][0-9A-ZÅÄÖ]{1,4}){0,3})\s+(?=\S)")


def heading_number(text: str) -> tuple[str | None, int | None]:
    """The heading's number or code and the level it implies, if it has one."""
    if m := NUMBERED.match(text):
        return m.group(1), m.group(1).count(".") + 1
    if m := CODED.match(text):
        return m.group(1), len(re.sub(r"\W", "", m.group(1)))
    if m := SINGLE_NUMBER.match(text):
        return m.group(1), 1
    return None, None
