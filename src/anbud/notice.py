"""A public tender's metadata, read from the procurement platform's notice page by code and shown to no one.

Blind intake needs a few facts about each tender before anyone has opened it: who is buying (the split's entity key),
the procurement reference (its family key), whether bids are still open (prospective), and the publication date (how
many tenders a model could have seen in training). This module finds the notice page among a delivery's files (saved
HTML, MHTML or a Safari web archive) and reads them by label.

Nothing here is printed or logged. Values go into the split as hashed keys and into the tender's local intake record;
the screen gets counts of which fields were found.

It also collects the documents the notice itself lists under an answer-key heading (questions and answers,
amendments, award), so intake can hold those files back even when their names say nothing, and marks the lines of
such sections, so intake can hold a notice page that carries them and put a copy without them in its place.

A page that can't be read counts as no page: reading never stops an intake.
"""

from __future__ import annotations

import contextlib
import email
import email.policy
import html
import io
import lzma
import plistlib
import re
import struct
import unicodedata
import zipfile
import zlib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import unquote

from anbud.answer_terms import classify, classify_path, fold

NOTICE_SUFFIXES = (".html", ".htm", ".xhtml", ".mht", ".mhtml", ".webarchive")
MAX_NOTICE_BYTES = 20 * 1024 * 1024
MAX_INNER_ZIP_BYTES = 200 * 1024 * 1024  # nested zips searched for a notice, in memory
MAX_LINES = 50_000
NOTICE_SCORE = 2  # a page with labels for at least this many fields is a notice, whatever its title says
BUYER_BLOCK = 12  # lines after the buyer's name searched first for its org number

# What reading a damaged, odd or unsupported zip member can raise: a damaged deflate stream raises zlib.error, which
# is not an OSError, and LZMA members raise their own error. Every reader of a delivered zip catches all of these.
ZIP_ERRORS: tuple[type[BaseException], ...] = (
    zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, RuntimeError, EOFError, OSError, ValueError,
    zlib.error, lzma.LZMAError, struct.error,
)  # fmt: skip

# Labels as they appear on the Swedish platforms and in eForms/TED, in order of preference: a value under an earlier
# label wins over one under a later label, wherever the two stand on the page. Matched on folded text (lower case, no
# accents, punctuation as spaces) at the start of a line; the longest matching label on a line is the one stripped.
LABELS: dict[str, tuple[str, ...]] = {
    "buyer": (
        "upphandlande myndighet", "upphandlande organisation", "upphandlande enhet", "upphandlande myndigheter",
        "officiellt namn", "official name", "contracting authority", "kopare", "kopares namn", "kopare namn",
        "bestallare", "bestallande organisation", "buyer", "organisation name", "organisation", "myndighet",
    ),
    "org_number": (
        "organisationsnummer", "org nr", "orgnr", "org nummer", "organisationsnr", "nationellt registreringsnummer",
        "registreringsnummer", "national registration number", "registration number", "organisation number",
    ),
    "reference": (
        "diarienummer", "dnr", "upphandlingens referensnummer", "referensnummer", "ert referensnummer",
        "internt referensnummer", "intern identifierare", "internt identifieringsnummer", "arendenummer",
        "upphandlingsnummer", "upphandlingsid", "upphandlings id", "reference number", "internal identifier",
    ),
    "published": (
        "publiceringsdatum", "publicerad", "publicerat", "publicerades", "datum for publicering", "annonsdatum",
        "annonserad", "publiceringsdag", "skickad for publicering", "publication date", "date of publication",
        "published",
    ),
    "deadline": (
        "sista anbudsdag", "sista dag for anbud", "sista dag att lamna anbud", "sista anbudsdatum",
        "sista dag for mottagande av anbud", "tidsfrist for mottagande av anbud", "anbudstidens utgang",
        "sista svarsdag", "sista dag for anbudsgivning", "anbud ska vara inkomna senast",
        "anbudet ska ha inkommit senast", "anbuden ska vara inkomna senast", "anbud ska ha kommit in senast",
        "anbudet ska ha kommit in senast", "sista dag for anbudsinlamning", "sista tid for anbud",
        "sista tid for mottagande av anbud", "sista tid for anbudsinlamning", "anbudsfrist", "anbudsinlamning",
        "anbudstid", "sista ansokningsdag", "sista dag for ansokan", "deadline for receipt of tenders",
        "time limit for receipt of tenders", "tender deadline", "deadline",
    ),
    "procedure": (
        "upphandlingsforfarande", "typ av forfarande", "forfarande", "upphandlingsform", "procedure type",
        "type of procedure", "procedure",
    ),
}  # fmt: skip

# A generic deadline label followed by one of these names another deadline (for questions, for requests to take
# part): it isn't the bid deadline, and taking it would close a tender that is still open.
_OTHER_DEADLINES = re.compile(
    r"^(?:tender )?deadline (?:for|to) (?!(?:the )?(?:receipt|submission) of (?:tenders|bids)|tenders|bids)"
)

PROCEDURES = {
    "oppet forfarande": "open",
    "open procedure": "open",
    "forenklat forfarande": "simplified",
    "selektivt forfarande": "selective",
    "restricted procedure": "selective",
    "forhandlat forfarande": "negotiated",
    "negotiated procedure": "negotiated",
    "konkurrenspraglad dialog": "dialogue",
    "competitive dialogue": "dialogue",
    "innovationspartnerskap": "innovation",
    "direktupphandling": "direct",
    "urvalsforfarande": "selective",
}

_MONTHS = {
    **{m: i for i, m in enumerate(
        ("januari", "februari", "mars", "april", "maj", "juni", "juli", "augusti", "september", "oktober",
         "november", "december"), 1)},
    **{m: i for i, m in enumerate(
        ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
         "november", "december"), 1)},
    **{m: i for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "maj", "jun", "jul", "aug", "sep", "okt", "nov", "dec"), 1)},
    "may": 5, "oct": 10, "sept": 9,
}  # fmt: skip

_TIME = r"(?:,?\s*(?:kl\.?\s*|at\s+)?(\d{1,2})[:.](\d\d))?"
_ISO = re.compile(r"(?<!\d)(20\d\d)-(\d\d)-(\d\d)(?:[ T]+(?:kl\.?\s*)?(\d{1,2})[:.](\d\d))?")
_DMY = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](20\d\d)(?:\s+(?:kl\.?\s*)?(\d{1,2})[:.](\d\d))?")
_WORDY = re.compile(r"(?<!\d)(\d{1,2})\.?\s+([a-zåäö]{3,9})\.?,?\s+(20\d\d)" + _TIME, re.I)
_MONTH_FIRST = re.compile(r"(?<!\w)([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d\d)(?!\d)" + _TIME, re.I)
_ORG = re.compile(r"(?<!\d)(\d{6})\s*-?\s*(\d{4})(?!\d)")
_CPV = re.compile(r"(?<!\d)(\d{8})(?:-\d)?(?!\d)")
DOC_SUFFIXES = frozenset(
    ".pdf .doc .docx .xls .xlsx .xlsm .zip .txt .rtf .odt .ods .dwg .ifc .png .jpg .jpeg .tif .tiff .html .htm".split()
)
_BLOCK_TAGS = frozenset(
    "p div br li tr td th dt dd h1 h2 h3 h4 h5 h6 section article table ul ol label header footer form fieldset "
    "legend caption blockquote pre hr summary details main aside nav dl tbody thead".split()
)
# Inline elements that stand between words: a page saved from a script-built platform often has no whitespace
# between a label and its value (<span>Sista anbudsdag</span><span>2026-12-15</span>).
_INLINE_GAPS = frozenset(
    "span b strong a time font small button input img abbr cite code var data output select option textarea bdi "
    "bdo".split()
)
_SKIP_TAGS = frozenset("script style noscript template svg math head".split())
_HEADINGS = {f"h{n}": n for n in range(1, 7)} | {"summary": 7, "legend": 7}
# Page chrome: navigation and side panels, and a header or footer outside the content. The platform's own name and
# org number live there, so field values are taken from it only when the content has none.
_ALWAYS_CHROME = frozenset({"nav", "aside"})
_EDGE_CHROME = frozenset({"header", "footer"})
_CONTENT = frozenset({"main", "article", "section"})


def _decode(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    head = data[:4096].decode("ascii", "replace")
    m = re.search(r"charset\s*=\s*[\"']?([A-Za-z0-9_\-]+)", head, re.I)
    if m:
        with contextlib.suppress(LookupError, ValueError):  # unknown, or a codec that can't decode bytes ("idna")
            return data.decode(m.group(1), "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")


def html_text(data: bytes, name: str) -> str | None:
    """The HTML inside a saved page: the page itself, the HTML parts of an MHTML file, or a web archive's main
    resource. None when there is none or it can't be read."""
    suffix = PurePosixPath(name).suffix.casefold()
    if suffix == ".webarchive" or data.startswith(b"bplist00"):
        try:
            main = plistlib.loads(data)["WebMainResource"]
            encoding = main.get("WebResourceTextEncodingName") or "utf-8"
            return bytes(main["WebResourceData"]).decode(encoding, "replace")
        except Exception:  # noqa: BLE001 - broken XML, wrong types, unknown codecs: no page
            return None
    if suffix in (".mht", ".mhtml") or data[:2048].lstrip().lower().startswith((b"mime-version", b"from:")):
        try:
            message = email.message_from_bytes(data, policy=email.policy.default)
            texts = []
            for part in message.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True) or b""
                    texts.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
            if texts:
                return "\n".join(texts)
        except Exception:  # noqa: BLE001 - a malformed message is no page
            return None
    return _decode(data)


class _Lines(HTMLParser):
    """Text as lines broken at block elements, the page title, file names listed in answer-key sections, and for each
    line whether it stands in an answer-key section or in page chrome."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.in_answer: list[bool] = []  # per line: inside a questions, amendments or award section
        self.in_chrome: list[bool] = []  # per line: navigation, a side panel, or a header or footer outside content
        self.current: list[str] = []
        self.title: list[str] = []
        self.skip = 0
        self.in_title = False
        self.title_done = False
        self.depth = dict.fromkeys(_ALWAYS_CHROME | _EDGE_CHROME | _CONTENT, 0)
        self.heading: tuple[int, list[str]] | None = None
        self.answer_section: tuple[int, str] | None = None  # (level, kind) of the open answer-key section
        self.listed: set[str] = set()
        self.first_heading: str | None = None
        self.link: list[str] | None = None
        self.href = ""

    @property
    def answer_lines(self) -> int:
        return sum(self.in_answer)

    @property
    def title_text(self) -> str:
        return " ".join("".join(self.title).split())

    def _chrome(self) -> bool:
        d = self.depth
        if any(d[t] for t in _ALWAYS_CHROME):
            return True
        return any(d[t] for t in _EDGE_CHROME) and not any(d[t] for t in _CONTENT)

    def _flush(self) -> None:
        text = " ".join("".join(self.current).split())
        if text and len(self.lines) < MAX_LINES:
            self.lines.append(text)
            self.in_answer.append(self.answer_section is not None)
            self.in_chrome.append(self._chrome())
            if self.answer_section and len(text) <= 200 and PurePosixPath(text).suffix.casefold() in DOC_SUFFIXES:
                self._list(text)  # a list item that is just a file name
        self.current = []

    def _gap(self) -> None:
        for buffer in (self.current, self.heading[1] if self.heading else None, self.link):
            if buffer is not None:
                buffer.append(" ")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            if tag != "head":  # the head is skipped for text, but its title is read
                self.skip += 1
            return
        if self.skip:
            return  # an icon's <title> inside an <svg> is not the page title
        if tag == "title":
            self.in_title = not self.title_done
            return
        if tag in _BLOCK_TAGS:
            self._flush()
        elif tag in _INLINE_GAPS:
            self._gap()
        if tag in self.depth:
            self.depth[tag] += 1
        if tag in _HEADINGS:
            self.heading = (_HEADINGS[tag], [])
        if tag == "a":
            self.link, self.href = [], dict(attrs).get("href") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if tag != "head" and self.skip:
                self.skip -= 1
            return
        if tag == "title":
            if self.in_title:
                self.in_title, self.title_done = False, True
            return
        if self.skip:
            return
        if tag == "a" and self.link is not None:
            if self.answer_section:
                self._list(" ".join("".join(self.link).split()))
                self._list(PurePosixPath(unquote(self.href.split("?")[0].split("#")[0])).name)
            self.link = None
        if tag in _HEADINGS and self.heading is not None:
            level, parts = self.heading
            text = " ".join("".join(parts).split())
            self.heading = None
            if text and self.first_heading is None and level == 1:
                self.first_heading = text
            if self.answer_section and level <= self.answer_section[0]:
                self.answer_section = None
            kind = classify(text) if text else None
            if kind:
                self.answer_section = (level, kind)
        if tag in _BLOCK_TAGS:
            self._flush()
        elif tag in _INLINE_GAPS:
            self._gap()
        if tag in self.depth and self.depth[tag]:
            self.depth[tag] -= 1

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title.append(data)
            return
        if self.skip:
            return
        self.current.append(data)
        if self.heading is not None:
            self.heading[1].append(data)
        if self.link is not None:
            self.link.append(data)

    def _list(self, name: str) -> None:
        stem = listed_stem(name)
        if len(stem) >= 3:
            self.listed.add(stem)

    def close(self) -> None:
        super().close()
        self._flush()


def listed_stem(name: str) -> str:
    """How a listed document and a delivered file are compared: folded, without a document extension, since a
    link's text often leaves it out."""
    name = unicodedata.normalize("NFC", name.strip())
    if PurePosixPath(name).suffix.casefold() in DOC_SUFFIXES:
        name = name[: -len(PurePosixPath(name).suffix)]
    return fold(name)


def _label_patterns() -> dict[str, list[re.Pattern[str]]]:
    return {f: [re.compile(rf"^{re.escape(label)}(?: |$)") for label in labels] for f, labels in LABELS.items()}


_PATTERNS = _label_patterns()
_ALL_LABELS = [p for ps in _PATTERNS.values() for p in ps]


def _is_label(folded: str) -> bool:
    return any(p.match(folded) for p in _ALL_LABELS)


@dataclass(frozen=True, order=True)
class _Candidate:
    """A field value found on a page. Sorts best first: content before chrome, then the label's preference, then the
    line's place on the page."""

    chrome: bool
    preference: int
    line: int
    value: str = field(compare=False)


def _values(page: _Lines, field_name: str) -> list[_Candidate]:
    """Candidate values for a field, best first: the rest of a labelled line, or the line after a label that stands
    alone."""
    found = []
    lines = page.lines
    folded_lines = [fold(line) for line in lines]
    for i, folded in enumerate(folded_lines):
        hits = [(len(p.pattern), n, m) for n, p in enumerate(_PATTERNS[field_name]) if (m := p.match(folded))]
        if not hits:
            continue
        if field_name == "deadline" and _OTHER_DEADLINES.match(folded):
            continue
        m = max(hits, key=lambda h: h[0])[2]  # the longest label is stripped...
        preference = min(n for _, n, _ in hits)  # ...and the most preferred one ranks the value
        chrome = page.in_chrome[i]
        rest = _unfold_words(lines[i], len(m.group(0).split()))
        if re.search(r"\w", rest):
            found.append(_Candidate(chrome, preference, i, rest))
            continue
        for j in (i + 1, i + 2):
            if j < len(lines) and not _is_label(folded_lines[j]):
                found.append(_Candidate(chrome, preference, j, lines[j]))
                break
    return sorted(found)


def _unfold_words(line: str, label_words: int) -> str:
    """Drop the first `label_words` folded words from the original line, however the punctuation fell."""
    count = 0
    for m in re.finditer(r"[^\W_]+", line):
        if fold(m.group(0)):
            count += len(fold(m.group(0)).split())
        if count >= label_words:
            return line[m.end() :].lstrip(" :-–—\t)|")
    return ""


def _dates(text: str) -> list[datetime]:
    """Every date in the text, in order of appearance."""
    found: list[tuple[int, datetime]] = []
    for regex, order in ((_ISO, "ymd"), (_DMY, "dmy"), (_WORDY, "dMy"), (_MONTH_FIRST, "Mdy")):
        for m in regex.finditer(text):
            try:
                if order == "ymd":
                    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                elif order == "dmy":
                    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                elif order == "dMy":
                    mo, d, y = _MONTHS.get(fold(m.group(2)), 0), int(m.group(1)), int(m.group(3))
                else:
                    mo, d, y = _MONTHS.get(fold(m.group(1)), 0), int(m.group(2)), int(m.group(3))
                hour, minute = (int(m.group(4)), int(m.group(5))) if m.group(4) else (0, 0)
                found.append((m.start(), datetime(y, mo, d, hour, minute)))
            except ValueError:
                continue
    return [d for _, d in sorted(found, key=lambda f: f[0])]


def parse_datetime(text: str) -> datetime | None:
    """The first date in the text."""
    dates = _dates(text)
    return dates[0] if dates else None


def _clean_name(value: str) -> str | None:
    value = " ".join(value.split())[:160].strip(" :-–—,;|")
    if not re.search(r"[^\W\d_]", value) or parse_datetime(value) or _is_label(fold(value)):
        return None
    return value


_PHONE = re.compile(r"(?<!\d)(?:\+46|0)\s?\d{1,3}[\s-]+\d{2,3}[\s-]?\d{2}[\s-]?\d{2}(?!\d)")
_EMAIL = re.compile(r"\S+@\S+")


def _reference(value: str) -> str | None:
    """A procurement reference: an identifier with a digit, not a date, a label, a phone number or a sentence."""
    ref = " ".join(value.split())[:80].strip(" :-–—,;|")
    if not re.search(r"\d", ref) or parse_datetime(ref) or _is_label(fold(ref)):
        return None
    if _PHONE.search(ref) or _EMAIL.search(ref):
        return None  # a contact line
    if sum(1 for w in ref.split() if w.isalpha() and len(w) > 3) > 3:
        return None  # prose, not an identifier
    return ref.split("  ")[0]


@dataclass
class Notice:
    """What the notice says, local only. `found` counts which fields were read."""

    buyer: str | None = None
    org_number: str | None = None  # ten digits
    reference: str | None = None
    published: date | None = None
    deadline: datetime | None = None  # the latest bid deadline on any page, answer pages included
    cpv: list[str] = field(default_factory=list)
    procedure: str | None = None
    framework: bool = False  # a framework agreement (ramavtal)
    pages: int = 0  # notice pages read
    listed: set[str] = field(default_factory=set)  # folded file names listed under answer-key headings
    answer_pages: set[str] = field(default_factory=set)  # member paths of pages that are themselves answer keys
    section_pages: set[str] = field(default_factory=set)  # member paths of notice pages with answer-key sections

    def found(self) -> dict[str, bool]:
        return {
            "buyer": self.buyer is not None,
            "org_number": self.org_number is not None,
            "reference": self.reference is not None,
            "published": self.published is not None,
            "deadline": self.deadline is not None,
            "cpv": bool(self.cpv),
            "procedure": self.procedure is not None,
        }

    def record(self) -> dict:
        """For the tender's local intake record (never printed)."""
        out = asdict(self)
        out["published"] = self.published.isoformat() if self.published else None
        out["deadline"] = self.deadline.isoformat() if self.deadline else None
        out["listed"] = sorted(self.listed)
        out["answer_pages"] = sorted(self.answer_pages)
        out["section_pages"] = sorted(self.section_pages)
        return out


def read_page(data: bytes, name: str) -> tuple[_Lines, int] | None:
    """The page's lines and a score: how many distinct fields have a label on it. None when it can't be read."""
    try:
        text = html_text(data, name)
        if text is None:
            return None
        parser = _Lines()
        parser.feed(text)
        parser.close()
    except Exception:  # noqa: BLE001 - an odd page is no page; one odd page never stops an intake
        return None
    folded = [fold(line) for line in parser.lines]
    score = sum(1 for patterns in _PATTERNS.values() if any(p.match(f) for p in patterns for f in folded))
    return parser, score


def page_kind(parts: tuple[str, ...], page: tuple[_Lines, int]) -> str | None:
    """The answer kind of a whole page, held whole in a dev inbox: by its path, its title or its first heading. A
    title that is the tender's name ("Kompletterande markarbeten etapp 2") doesn't hold the page: its weak word stands
    with ordinary words. A held page that carries the notice's labels is still read for the notice."""
    parser, _ = page
    return (
        classify_path(parts)
        or classify(parser.title_text)
        or (classify(parser.first_heading) if parser.first_heading else None)
    )


def without_answer_sections(parser: _Lines) -> str:
    """A plain copy of a page with its answer-key sections left out: the title and every other line, one paragraph
    each. It takes the page's place in a dev inbox; the page itself is held with the answers."""
    kept = (line for line, held in zip(parser.lines, parser.in_answer, strict=True) if not held)
    body = "".join(f"<p>{html.escape(line)}</p>\n" for line in kept)
    head = f'<meta charset="utf-8"><title>{html.escape(parser.title_text)}</title>'
    return f"<!DOCTYPE html>\n<html><head>{head}</head><body>\n{body}</body></html>\n"


def _org(value: str) -> str | None:
    m = _ORG.search(value)
    return m.group(1) + m.group(2) if m else None


def extract(pages: list[tuple[_Lines, int]]) -> Notice:
    """The fields, each from the best-scoring page that has it. The deadline is the latest over every page."""
    notice = Notice(pages=len(pages))
    ranked = sorted(pages, key=lambda p: -p[1])
    for parser, _ in ranked:
        notice.listed |= parser.listed
        lines = parser.lines
        if notice.buyer is None:
            for c in _values(parser, "buyer"):
                if name := _clean_name(c.value):
                    notice.buyer = name
                    # The buyer's own block first: the platform's org number may stand anywhere else on the page.
                    block = [j for j in range(c.line, min(len(lines), c.line + BUYER_BLOCK)) if not parser.in_chrome[j]]
                    near = [o for o in (_org(v.value) for v in _values(parser, "org_number") if v.line in block) if o]
                    near = near or [o for o in (_org(lines[j]) for j in block) if o]
                    if near and notice.org_number is None:
                        notice.org_number = near[0]
                    break
        if notice.org_number is None:
            labelled = [c for c in _values(parser, "org_number") if not c.chrome]
            notice.org_number = next((o for o in (_org(c.value) for c in labelled) if o), None)
        if notice.reference is None:
            notice.reference = next((r for r in (_reference(c.value) for c in _values(parser, "reference")) if r), None)
        if notice.published is None:
            stamp = next((d for d in (parse_datetime(c.value) for c in _values(parser, "published")) if d), None)
            notice.published = stamp.date() if stamp else None
        if notice.procedure is None:
            for value in [*(c.value for c in _values(parser, "procedure")), *lines]:
                folded = fold(value)
                kind = next((k for phrase, k in PROCEDURES.items() if phrase in folded), None)
                if kind:
                    notice.procedure = kind
                    break
        if not notice.cpv:
            codes = []
            for i, line in enumerate(lines):
                near_text = " ".join(lines[max(0, i - 1) : i + 2])
                if "cpv" in fold(near_text).split() or re.search(r"\d{8}-\d", line):
                    codes += [c for c in _CPV.findall(line) if 3 <= int(c[:2]) <= 98]
            notice.cpv = list(dict.fromkeys(codes))
        notice.framework = notice.framework or any("ramavtal" in fold(line).split() for line in lines[:2000])
    if notice.org_number is None:  # a public body's number in the best page's content: they start with 2
        for parser, _ in ranked[:1]:
            for line, chrome in zip(parser.lines, parser.in_chrome, strict=True):
                if not chrome and (m := _ORG.search(line)) and m.group(1).startswith("2"):
                    notice.org_number = m.group(1) + m.group(2)
                    break
    notice.deadline = latest_deadline(pages)
    return notice


def latest_deadline(pages: list[tuple[_Lines, int]]) -> datetime | None:
    """The latest bid deadline on any of the pages: every labelled value, every date in it (a range gives its end).
    Later is the safe direction: a tender wrongly seen as open is sealed, one wrongly seen as closed could be opened."""
    dates = [d for parser, _ in pages for c in _values(parser, "deadline") for d in _dates(c.value)]
    return max(dates) if dates else None


def entry_name(info: zipfile.ZipInfo) -> str:
    """Zips written without the UTF-8 flag are read as cp437. If the bytes are really UTF-8 (a Mac without the
    flag), read them as UTF-8; otherwise cp437, which spells å, ä and ö like the Swedish Windows code page does."""
    name = info.filename
    if not info.flag_bits & 0x800:
        with contextlib.suppress(UnicodeEncodeError, UnicodeDecodeError):
            name = name.encode("cp437").decode("utf-8")
    return unicodedata.normalize("NFC", name)


def member_parts(info: zipfile.ZipInfo, prefix: tuple[str, ...] = ()) -> tuple[str, ...]:
    """A zip member's path as intake unpacks it: the decoded name split at either slash, empty and '.' parts left
    out, under `prefix`."""
    return (*prefix, *(p for p in entry_name(info).replace("\\", "/").split("/") if p not in ("", ".")))


def nested_prefix(parts: tuple[str, ...]) -> tuple[str, ...]:
    """Where a nested zip's members land: a folder named after the zip, without `.zip`."""
    return (*parts[:-1], PurePosixPath(parts[-1]).stem)


def _members(zf: zipfile.ZipFile, prefix: tuple[str, ...], depth: int) -> list[tuple[tuple[str, ...], bytes]]:
    """Candidate notice pages in a zip, and in zips inside it, as (path parts, bytes)."""
    out = []
    for info in zf.infolist():
        if info.is_dir() or info.flag_bits & 0x1:
            continue
        parts = member_parts(info, prefix)
        if len(parts) == len(prefix):
            continue
        suffix = PurePosixPath(parts[-1]).suffix.casefold()
        try:
            if suffix in NOTICE_SUFFIXES and info.file_size <= MAX_NOTICE_BYTES:
                out.append((parts, zf.read(info)))
            elif suffix == ".zip" and depth < 3 and info.file_size <= MAX_INNER_ZIP_BYTES:
                with zipfile.ZipFile(io.BytesIO(zf.read(info))) as inner:
                    out += _members(inner, nested_prefix(parts), depth + 1)
        except ZIP_ERRORS:
            continue
    return out


def from_zip(path: str | object) -> Notice:
    """Read the notice from a delivered zip without unpacking it to disk. A damaged zip gives an empty notice."""
    try:
        with zipfile.ZipFile(path) as zf:  # type: ignore[arg-type]
            members = _members(zf, (), 0)
    except ZIP_ERRORS:
        return Notice()
    return from_pages(members)


def from_pages(members: list[tuple[tuple[str, ...], bytes]]) -> Notice:
    notice_pages = []
    readable = []
    answer_pages: set[str] = set()
    section_pages: set[str] = set()
    for parts, data in members:
        page = read_page(data, parts[-1])
        if page is None:
            continue
        readable.append(page)
        name = "/".join(parts)
        if page_kind(parts, page):
            answer_pages.add(name)  # an answer key: held whole in a dev tender
            if page[1] < NOTICE_SCORE:
                continue  # and not the notice
        elif page[0].answer_lines:
            section_pages.add(name)
        notice_pages.append(page)
    notice = extract(notice_pages)
    notice.deadline = latest_deadline(readable)  # an amendment or Q&A page may have moved it
    notice.answer_pages = answer_pages
    notice.section_pages = section_pages
    return notice
