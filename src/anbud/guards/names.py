"""The names guard. It finds listed customer, project and people's names without ever printing them.

The plain list (`~/estim-anbud-data/names.txt`) stays on the Mac. CI gets only `hash-names` output: a random key and an
HMAC of each normalised entry, so GitHub never holds a readable name. Both paths use the same matcher: every window of
1 to N words in the text is normalised, hashed with the key and looked up.

List format: one entry per line. A line starting with `#` is a comment; nothing else on an entry line is ignored, so
notes go on their own line. A leading `=` makes the entry case-sensitive (for names that are also ordinary words).

Matching:
- by whole words, with accents optional; invisible characters (soft hyphens, zero-width spaces, variation selectors)
  are read both as nothing and as a word break;
- camelCase and letter-digit joins are split ("FiktivAb2" also reads as "Fiktiv Ab 2"), except in hex strings;
- a multi-word entry also matches written as one word ("fiktivbolagetab.se");
- a Swedish genitive "s" on the last word still matches ("Fiktivbolagets").
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

HEADER = "anbud-names-v1"
MAX_WORDS = 8
_TOKEN = re.compile(r"[^\W_]+")
_FORBIDDEN_IN_ENTRY = set("#()[],;/")
_HEX = re.compile(r"[0-9a-fA-F]{7,}")


class NamesMissing(Exception):
    """The list couldn't be loaded. The guard fails closed. Messages never contain a name."""


@dataclass(frozen=True)
class Entry:
    tokens: tuple[str, ...]
    case_sensitive: bool


def _clean(text: str, joiner: str = "") -> str:
    """NFKC, then format characters (Cf) become `joiner` and leftover combining marks (Mn) go."""
    text = unicodedata.normalize("NFKC", text)
    if text.isascii():
        return text
    out = []
    for c in text:
        category = unicodedata.category(c)
        if category == "Cf":
            out.append(joiner)
        elif category != "Mn":
            out.append(c)
    return "".join(out)


def _split_joins(token: str) -> list[str]:
    """Split at lower-to-upper case changes, before the last capital of a run followed by lower case, and at
    letter-digit changes: FiktivbolagetAb2 -> Fiktivbolaget Ab 2, XYZFiktiva -> XYZ Fiktiva. Hex strings (hashes)
    stay whole, or their letter runs would match short entries."""
    if _HEX.fullmatch(token):
        return [token]
    parts: list[str] = []
    start = 0
    for i in range(1, len(token)):
        prev, cur = token[i - 1], token[i]
        nxt = token[i + 1] if i + 1 < len(token) else ""
        if (
            (prev.islower() and cur.isupper())
            or (prev.isupper() and cur.isupper() and nxt.islower())
            or (prev.isdigit() != cur.isdigit())
        ):
            parts.append(token[start:i])
            start = i
    parts.append(token[start:])
    return parts


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c))


def _forms(tokens: tuple[str, ...], case_sensitive: bool) -> set[tuple[str, ...]]:
    base = tokens if case_sensitive else tuple(t.casefold() for t in tokens)
    stripped = tuple(_strip_accents(t) for t in base)
    forms = {base, stripped}
    if len(base) > 1:
        forms |= {("".join(base),), ("".join(stripped),)}
    return forms


def _digest(key: bytes, mode: str, tokens: tuple[str, ...]) -> str:
    message = (mode + "\x1e" + "\x1f".join(tokens)).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def parse_plain(text: str) -> list[Entry]:
    entries = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = _clean(raw).strip()
        if not line or line.startswith("#"):
            continue
        case_sensitive = line.startswith("=")
        if case_sensitive:
            line = line[1:].strip()
        if _FORBIDDEN_IN_ENTRY & set(line):
            raise NamesMissing(f"names list line {number}: no comments or brackets on an entry line; use a # line")
        tokens = tuple(_TOKEN.findall(line))
        if not tokens:
            continue
        if len(tokens) > MAX_WORDS:
            raise NamesMissing(f"names list line {number}: more than {MAX_WORDS} words; shorten it")
        entries.append(Entry(tokens, case_sensitive))
    return entries


def hash_entries(entries: list[Entry], key: bytes) -> list[tuple[str, int, str]]:
    records = set()
    for entry in entries:
        mode = "s" if entry.case_sensitive else "i"
        for form in _forms(entry.tokens, entry.case_sensitive):
            records.add((mode, len(form), _digest(key, mode, form)))
    return sorted(records, key=lambda r: r[2])


def render_hashed(entries: list[Entry], key: bytes | None = None) -> str:
    key = key or secrets.token_bytes(32)
    lines = [HEADER, f"key {key.hex()}"]
    lines += [f"{mode} {n} {digest}" for mode, n, digest in hash_entries(entries, key)]
    return "\n".join(lines) + "\n"


def _token_streams(text: str) -> list[tuple[list[str], list[int]]]:
    """The plain word stream, plus one with camelCase and letter-digit joins split, each with line numbers."""
    line_starts = [0] + [m.end() for m in re.finditer("\n", text)]
    plain: list[str] = []
    split: list[str] = []
    plain_lines: list[int] = []
    split_lines: list[int] = []
    line = 0
    for match in _TOKEN.finditer(text):
        while line + 1 < len(line_starts) and line_starts[line + 1] <= match.start():
            line += 1
        plain.append(match.group())
        plain_lines.append(line + 1)
        parts = _split_joins(match.group())
        split.extend(parts)
        split_lines.extend([line + 1] * len(parts))
    streams = [(plain, plain_lines)]
    if split != plain:
        streams.append((split, split_lines))
    return streams


class Matcher:
    def __init__(self, key: bytes, records: list[tuple[str, int, str]]) -> None:
        if not records:
            raise NamesMissing("the names list is empty")
        self._key = key
        self._digests = {(mode, digest) for mode, _, digest in records}
        self._sizes = {mode: sorted({n for m, n, _ in records if m == mode}) for mode in ("i", "s")}

    @classmethod
    def from_entries(cls, entries: list[Entry]) -> Matcher:
        key = secrets.token_bytes(32)
        return cls(key, hash_entries(entries, key))

    @classmethod
    def from_hashed(cls, text: str) -> Matcher:
        lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
        if len(lines) < 2 or lines[0] != HEADER or not lines[1].startswith("key "):
            raise NamesMissing("the hashed names list is malformed")
        try:
            key = bytes.fromhex(lines[1][4:])
            records = []
            for line in lines[2:]:
                mode, n, digest = line.split()
                if mode not in ("i", "s") or not 1 <= int(n) <= MAX_WORDS or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError
                records.append((mode, int(n), digest))
        except ValueError:
            raise NamesMissing("the hashed names list is malformed") from None
        return cls(key, records)

    def _hit(self, mode: str, window: tuple[str, ...]) -> bool:
        candidates = [window, tuple(_strip_accents(t) for t in window)]
        last = window[-1]
        if len(last) > 2 and last[-1] in "sS" and last[-2] not in "sS":  # genitive: Fiktivbolagets, not "class"
            trimmed = (*window[:-1], last[:-1])
            candidates += [trimmed, tuple(_strip_accents(t) for t in trimmed)]
        return any((mode, _digest(self._key, mode, c)) in self._digests for c in dict.fromkeys(candidates))

    def find_lines(self, text: str) -> list[int]:
        """1-based line numbers where a listed name starts. Windows may cross line breaks."""
        hits: set[int] = set()
        cleaned = dict.fromkeys((_clean(text), _clean(text, " ")))
        for tokens, lines in (stream for c in cleaned for stream in _token_streams(c)):
            for mode, sizes in self._sizes.items():
                if not sizes:
                    continue
                forms = tokens if mode == "s" else [t.casefold() for t in tokens]
                for n in sizes:
                    for i in range(len(forms) - n + 1):
                        if lines[i] not in hits and self._hit(mode, tuple(forms[i : i + n])):
                            hits.add(lines[i])
        return sorted(hits)


def load_matcher(env: Mapping[str, str] | None = None, names_file: Path | None = None) -> Matcher:
    """CI uses ANBUD_NAMES_HMAC. Locally the plain list is read. Anything missing fails closed."""
    env = os.environ if env is None else env
    hashed = env.get("ANBUD_NAMES_HMAC", "")
    if hashed.strip():
        return Matcher.from_hashed(hashed)
    if env.get("CI"):
        raise NamesMissing("ANBUD_NAMES_HMAC is not set in CI")
    if names_file is None:
        from anbud.paths import names_file as default_names_file

        names_file = default_names_file()
    try:
        text = names_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise NamesMissing(f"no names list at {names_file}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise NamesMissing(f"the names list can't be read as UTF-8 text ({type(exc).__name__})") from None
    return Matcher.from_entries(parse_plain(text))
