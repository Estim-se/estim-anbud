"""The intake gate: stop on security markings, mask personal data before any text reaches a prompt.

- **Stop** on a HEMLIG marking (upper case, as classification markings are written) or any mention of
  säkerhetsskydd. A person then decides whether the tender may be processed at all.
- **Mask** personnummer and samordningsnummer (checksum verified), email addresses and IBANs (checksum verified)
  everywhere, and phone numbers only right after a label such as tel, mobil or telefon, or right after an email
  address (a contact line).
- **Never mask** numeric spreadsheet cells, or anything shorter than eight digits, so E-numbers and quantities survive
  for the number checks.
- **Names in known structures:** spreadsheet cells under a column headed namn, förnamn, efternamn, kontaktperson,
  hyresgäst or boende become role tokens, one per distinct name in the file. Names in free text and in PDF or Word
  tables are not masked; that limit is stated to the customer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from anbud.prep.model import MarkingKind, MaskKind

_HEMLIG = re.compile(r"\bHEMLIG\b")
_SAKERHETSSKYDD = re.compile(r"s[äa]kerhetsskydd", re.IGNORECASE)

_PNR = re.compile(r"(?<![\d-])((?:19|20)?\d{2})(\d{2})(\d{2})([-+]?)(\d{4})(?![\d-])")
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+(?![\w-])")
_IBAN_START = re.compile(r"(?<![A-Z0-9])[A-Z]{2}\d{2}")
_IBAN_CHAR = re.compile(r"[A-Z0-9]")
_PHONE_LABEL = re.compile(
    r"\b(?:tel|tfn|telefon|telefonnummer|mobil|mobilnummer|mobiltelefon|mob|växel|direkt|fax)\b\.?(?:\s*nr\.?)?\s*[:.]?\s*",
    re.IGNORECASE,
)
_PHONE = re.compile(r"(?:\+\s?46|0)[\d\s()\-–—]{5,17}\d")  # hyphen, en dash or em dash between groups
_PHONE_SEP = re.compile(r"\s*(?:,|/|och|eller)\s*")
_AFTER_EMAIL = re.compile(r"\s*[,;/]?\s*")
# Column headers that hold people's names. Compounds for rooms, items, files or companies (rumsnamn, artikelnamn,
# företagsnamn) don't match: only a bare "namn" or a person compound does.
NAME_HEADER = re.compile(
    r"(?<![a-zåäö])(?:för|efter|tilltals|mellan|kontakt)?namn(?![a-zåäö])"
    r"|(?<![a-zåäö])(?:hyresgäst(?:er)?|kontaktperson(?:er)?|boende)(?![a-zåäö])",
    re.IGNORECASE,
)
_HYPHEN_BREAK = re.compile(r"-\s+")

TOKENS: dict[MaskKind, str] = {
    "personnummer": "[personnummer]",
    "email": "[e-post]",
    "iban": "[IBAN]",
    "phone": "[telefon]",
    "name": "[namn {n}]",
}


def markings(text: str) -> list[MarkingKind]:
    """Checked on the text as given and with line-break hyphens closed up ("säkerhets- skydd")."""
    joined = _HYPHEN_BREAK.sub("", text)
    found: list[MarkingKind] = []
    if _HEMLIG.search(text) or _HEMLIG.search(joined):
        found.append("hemlig")
    if _SAKERHETSSKYDD.search(text) or _SAKERHETSSKYDD.search(joined):
        found.append("sakerhetsskydd")
    return found


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(digits):
        d = int(ch) * (2 if i % 2 == 0 else 1)
        total += d - 9 if d > 9 else d
    return total % 10 == 0


def _is_pnr(m: re.Match[str]) -> bool:
    year, month, day, _, last = m.groups()
    if not 1 <= int(month) <= 12:
        return False  # organisation numbers have 20 or more in the month position
    d = int(day)
    if not (1 <= d <= 31 or 61 <= d <= 91):  # 61-91: samordningsnummer
        return False
    return _luhn(year[-2:] + month + day + last)


def _iban_ok(raw: str) -> bool:
    s = raw.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    moved = s[4:] + s[:4]
    return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1


def _ibans(text: str) -> list[tuple[int, int]]:
    """IBANs, written compact or in groups with single spaces. The longest checksum-valid prefix wins, so a BIC, a
    currency or a year after the number never hides it."""
    found = []
    pos = 0
    while m := _IBAN_START.search(text, pos):
        ends = []  # end offset after each further character, skipping single spaces
        i, count = m.end(), 4
        while i < len(text) and count < 34:
            j = i + 1 if text[i] == " " else i
            if j < len(text) and _IBAN_CHAR.fullmatch(text[j]) and not (j > i and text[i - 1] == " "):
                count += 1
                i = j + 1
                ends.append(i)
            else:
                break
        hit = next((e for e in reversed(ends) if not _IBAN_CHAR.match(text, e) and _iban_ok(text[m.start() : e])), None)
        if hit is None:
            hit = next((e for e in reversed(ends) if _iban_ok(text[m.start() : e])), None)
        if hit is not None:
            found.append((m.start(), hit))
            pos = hit
        else:
            pos = m.start() + 1
    return found


@dataclass(frozen=True)
class Span:
    kind: MaskKind
    start: int  # in the masked text
    end: int
    original: str


def _phone_digits(candidate: str) -> bool:
    """Eight to thirteen digits: shorter is an E-number or a quantity, longer isn't a phone number."""
    return 8 <= sum(c.isdigit() for c in candidate) <= 13


def _find(text: str) -> list[tuple[int, int, MaskKind]]:
    hits: list[tuple[int, int, MaskKind]] = []
    emails = list(_EMAIL.finditer(text))
    hits += [(m.start(), m.end(), "email") for m in emails]
    for email in emails:  # a contact line: the number right after an address is a phone even without a label
        if (m := _PHONE.match(text, _AFTER_EMAIL.match(text, email.end()).end())) and _phone_digits(m.group()):
            hits.append((m.start(), m.end(), "phone"))
    hits += [(start, end, "iban") for start, end in _ibans(text)]
    hits += [(m.start(), m.end(), "personnummer") for m in _PNR.finditer(text) if _is_pnr(m)]
    for label in _PHONE_LABEL.finditer(text):
        pos = label.end()
        while m := _PHONE.match(text, pos):
            if _phone_digits(m.group()):
                hits.append((m.start(), m.end(), "phone"))
            sep = _PHONE_SEP.match(text, m.end())
            if not sep:
                break
            pos = sep.end()
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    kept: list[tuple[int, int, MaskKind]] = []
    for hit in hits:
        if not kept or hit[0] >= kept[-1][1]:
            kept.append(hit)
    return kept


SEP = " | "


def find_spans(text: str) -> list[tuple[int, int, MaskKind]]:
    """What the masking would replace in `text`, as (start, end, kind) offsets into `text` itself."""
    return _find(text)


@dataclass(frozen=True)
class Cell:
    col: str
    text: str
    numeric: bool = False
    name_column: bool = False


class Masker:
    """Masks one file. Name tokens are numbered per file, the same name always getting the same token."""

    def __init__(self) -> None:
        self._names: dict[str, int] = {}

    def text(self, text: str) -> tuple[str, list[Span]]:
        out: list[str] = []
        spans: list[Span] = []
        pos = 0
        length = 0
        for start, end, kind in _find(text):
            out.append(text[pos:start])
            length += start - pos
            token = TOKENS[kind]
            spans.append(Span(kind, length, length + len(token), text[start:end]))
            out.append(token)
            length += len(token)
            pos = end
        out.append(text[pos:])
        return "".join(out), spans

    def name(self, value: str) -> tuple[str, Span]:
        n = self._names.setdefault(value.casefold(), len(self._names) + 1)
        token = TOKENS["name"].format(n=n)
        return token, Span("name", 0, len(token), value)

    def cells(self, cells: list[Cell]) -> tuple[str, list[Span]]:
        """A sheet row: string cells are masked, numeric cells never; cells in a name column become role tokens."""
        parts: list[str] = []
        spans: list[Span] = []
        offset = 0
        for i, cell in enumerate(cells):
            if i:
                offset += len(SEP)
            if cell.numeric:
                masked, cell_spans = cell.text, []
            elif cell.name_column:
                masked, span = self.name(cell.text)
                cell_spans = [span]
            else:
                masked, cell_spans = self.text(cell.text)
            spans += [Span(s.kind, s.start + offset, s.end + offset, s.original) for s in cell_spans]
            parts.append(masked)
            offset += len(masked)
        return SEP.join(parts), spans
