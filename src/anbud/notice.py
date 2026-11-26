"""A public tender's metadata, read from the procurement platform's notice page by code and shown to no one.

Blind intake needs a few facts about each tender before anyone has opened it: who is buying (the split's entity key),
the procurement reference (its family key), whether bids are still open (prospective), and the publication date (how
many tenders a model could have seen in training). This module finds the notice page among a delivery's files (saved
HTML, MHTML or a Safari web archive) and reads them by label.

Nothing here is printed or logged. Values go into the split as hashed keys and into the tender's local intake record;
the screen gets counts of which fields were found.

It also collects the documents the notice itself lists under an answer-key heading (questions and answers,
amendments, award), so intake can hold those files back even when their names say nothing.
"""

from __future__ import annotations

import email
import email.policy
import io
import plistlib
import re
import unicodedata
import zipfile
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

# Labels as they appear on the Swedish platforms and in eForms/TED, in order of preference. Matched on folded text
# (lower case, no accents, punctuation as spaces) at the start of a line.
LABELS: dict[str, tuple[str, ...]] = {
    "buyer": (
        "upphandlande myndighet", "upphandlande organisation", "upphandlande enhet", "upphandlande myndigheter",
        "kopare", "kopares namn", "kopare namn", "officiellt namn", "bestallare", "bestallande organisation",
        "organisation", "myndighet", "upphandlare", "contracting authority", "buyer", "official name",
        "organisation name",
    ),
    "org_number": (
        "organisationsnummer", "org nr", "orgnr", "org nummer", "organisationsnr", "nationellt registreringsnummer",
        "registreringsnummer", "national registration number", "organisation number",
    ),
    "reference": (
        "diarienummer", "dnr", "upphandlingens referensnummer", "referensnummer", "ert referensnummer",
        "internt referensnummer", "intern identifierare", "internt identifieringsnummer", "arendenummer",
        "upphandlingsnummer", "upphandlingsid", "upphandlings id", "referens", "reference number", "reference",
        "internal identifier",
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
        "anbudet ska ha inkommit senast",
        "anbudstid", "sista ansokningsdag", "sista dag for ansokan", "tender deadline", "deadline",
        "time limit for receipt of tenders",
    ),
    "procedure": (
        "upphandlingsforfarande", "typ av forfarande", "forfarande", "upphandlingsform", "procedure type",
        "type of procedure", "procedure",
    ),
}  # fmt: skip

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
    "may": 5, "oct": 10,
}  # fmt: skip

_ISO = re.compile(r"(?<!\d)(20\d\d)-(\d\d)-(\d\d)(?:[ T]+(?:kl\.?\s*)?(\d{1,2})[:.](\d\d))?")
_DMY = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](20\d\d)(?:\s+(?:kl\.?\s*)?(\d{1,2})[:.](\d\d))?")
_WORDY = re.compile(r"(?<!\d)(\d{1,2})\s+([a-zåäö]{3,9})\.?\s+(20\d\d)(?:\s+(?:kl\.?\s*)?(\d{1,2})[:.](\d\d))?", re.I)
_ORG = re.compile(r"(?<!\d)(\d{6})\s*-?\s*(\d{4})(?!\d)")
_CPV = re.compile(r"(?<!\d)(\d{8})(?:-\d)?(?!\d)")
DOC_SUFFIXES = frozenset(
    ".pdf .doc .docx .xls .xlsx .xlsm .zip .txt .rtf .odt .ods .dwg .ifc .png .jpg .jpeg .tif .tiff .html .htm".split()
)
_BLOCK_TAGS = frozenset(
    "p div br li tr td th dt dd h1 h2 h3 h4 h5 h6 section article table ul ol label header footer form fieldset "
    "legend caption blockquote pre hr summary details main aside nav dl tbody thead".split()
)
_SKIP_TAGS = frozenset("script style noscript template svg math head".split())
_HEADINGS = {f"h{n}": n for n in range(1, 7)} | {"summary": 7, "legend": 7}


def _decode(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    head = data[:4096].decode("ascii", "replace")
    m = re.search(r"charset\s*=\s*[\"']?([A-Za-z0-9_\-]+)", head, re.I)
    if m:
        try:
            return data.decode(m.group(1), "replace")
        except LookupError:
            pass
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")


def html_text(data: bytes, name: str) -> str | None:
    """The HTML inside a saved page: the page itself, the HTML parts of an MHTML file, or a web archive's main
    resource. None when there is none."""
    suffix = PurePosixPath(name).suffix.casefold()
    if suffix == ".webarchive" or data.startswith(b"bplist00"):
        try:
            archive = plistlib.loads(data)
            main = archive["WebMainResource"]
            encoding = main.get("WebResourceTextEncodingName") or "utf-8"
            return bytes(main["WebResourceData"]).decode(encoding, "replace")
        except (ValueError, KeyError, TypeError, LookupError, plistlib.InvalidFileException):
            return None
    if suffix in (".mht", ".mhtml") or data[:2048].lstrip().lower().startswith((b"mime-version", b"from:")):
        try:
            message = email.message_from_bytes(data, policy=email.policy.default)
            parts = [p for p in message.walk() if p.get_content_type() == "text/html"]
            texts = []
            for part in parts:
                payload = part.get_payload(decode=True) or b""
                texts.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
            if texts:
                return "\n".join(texts)
        except (ValueError, LookupError, TypeError, AttributeError):
            return None
    return _decode(data)


class _Lines(HTMLParser):
    """Text as lines broken at block elements, the page title, and file names listed in answer-key sections."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.current: list[str] = []
        self.title: list[str] = []
        self.skip = 0
        self.in_title = False
        self.heading: tuple[int, list[str]] | None = None
        self.answer_section: tuple[int, str] | None = None  # (level, kind) of the open answer-key section
        self.listed: set[str] = set()
        self.first_heading: str | None = None
        self.link: list[str] | None = None
        self.href = ""

    def _flush(self) -> None:
        text = " ".join("".join(self.current).split())
        if text and len(self.lines) < MAX_LINES:
            self.lines.append(text)
            if self.answer_section and len(text) <= 200 and PurePosixPath(text).suffix.casefold() in DOC_SUFFIXES:
                self._list(text)  # a list item that is just a file name
        self.current = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            if tag == "head":
                return  # the head is skipped for text, but its title is read
            self.skip += 1
            return
        if tag == "title":
            self.in_title = True
            return
        if self.skip:
            return
        if tag in _BLOCK_TAGS:
            self._flush()
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
            self.in_title = False
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


def _label_patterns() -> dict[str, list[tuple[re.Pattern[str], int]]]:
    out: dict[str, list[tuple[re.Pattern[str], int]]] = {}
    for field_name, labels in LABELS.items():
        out[field_name] = [(re.compile(rf"^{re.escape(label)}(?: |$)"), len(label)) for label in labels]
    return out


_PATTERNS = _label_patterns()
_ALL_LABELS = [p for ps in _PATTERNS.values() for p, _ in ps]


def _is_label(folded: str) -> bool:
    return any(p.match(folded) for p in _ALL_LABELS)


def _strip_label(line: str, label_words: int) -> str:
    """The line after its label: as many words as the folded label had, then separators."""
    words = line.split()
    rest = " ".join(words[label_words:]) if label_words < len(words) else ""
    # A label word may carry the colon ("Diarienummer:"); the folded count already skipped it.
    return rest.lstrip(" :-–—\t")


def _values(lines: list[str], field_name: str) -> list[str]:
    """Candidate values for a field: the rest of a labelled line, or the line after a label that stands alone."""
    found = []
    folded_lines = [fold(line) for line in lines]
    for i, folded in enumerate(folded_lines):
        for pattern, _ in _PATTERNS[field_name]:
            m = pattern.match(folded)
            if not m:
                continue
            label_words = len(m.group(0).split())
            rest = _strip_label(_unfold_words(lines[i], label_words), 0)
            if rest:
                found.append(rest)
            for j in (i + 1, i + 2):
                if j < len(lines) and not _is_label(folded_lines[j]):
                    found.append(lines[j])
                    break
            break
    return found


def _unfold_words(line: str, label_words: int) -> str:
    """Drop the first `label_words` folded words from the original line, however the punctuation fell."""
    count = 0
    for m in re.finditer(r"[^\W_]+", line):
        if fold(m.group(0)):
            count += len(fold(m.group(0)).split())
        if count >= label_words:
            return line[m.end() :].lstrip(" :-–—\t)")
    return ""


def parse_datetime(text: str) -> datetime | None:
    for regex, order in ((_ISO, "ymd"), (_DMY, "dmy"), (_WORDY, "dMy")):
        m = regex.search(text)
        if not m:
            continue
        try:
            if order == "ymd":
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            elif order == "dmy":
                d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            else:
                month = _MONTHS.get(fold(m.group(2)))
                if month is None:
                    continue
                d, mo, y = int(m.group(1)), month, int(m.group(3))
            hour, minute = (int(m.group(4)), int(m.group(5))) if m.group(4) else (0, 0)
            return datetime(y, mo, d, hour, minute)
        except ValueError:
            continue
    return None


def _clean_name(value: str) -> str | None:
    value = " ".join(value.split())[:160].strip(" :-–—,;")
    if not re.search(r"[^\W\d_]", value) or parse_datetime(value) or _is_label(fold(value)):
        return None
    return value


@dataclass
class Notice:
    """What the notice says, local only. `found` counts which fields were read."""

    buyer: str | None = None
    org_number: str | None = None  # ten digits
    reference: str | None = None
    published: date | None = None
    deadline: datetime | None = None
    cpv: list[str] = field(default_factory=list)
    procedure: str | None = None
    framework: bool = False  # a framework agreement (ramavtal)
    pages: int = 0  # notice pages read
    listed: set[str] = field(default_factory=set)  # folded file names listed under answer-key headings
    answer_pages: set[str] = field(default_factory=set)  # member names of pages that are themselves answer keys

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
        return out


def read_page(data: bytes, name: str) -> tuple[_Lines, int] | None:
    """The page's lines and a score: how many distinct fields have a label on it."""
    text = html_text(data, name)
    if text is None:
        return None
    parser = _Lines()
    try:
        parser.feed(text)
        parser.close()
    except (ValueError, AssertionError, RecursionError):
        return None
    folded = [fold(line) for line in parser.lines]
    score = sum(1 for patterns in _PATTERNS.values() if any(p.match(f) for p, _ in patterns for f in folded))
    return parser, score


def extract(pages: list[tuple[_Lines, int]]) -> Notice:
    """The fields, each from the best-scoring page that has it."""
    notice = Notice(pages=len(pages))
    ranked = sorted(pages, key=lambda p: -p[1])
    for parser, _ in ranked:
        notice.listed |= parser.listed
        lines = parser.lines
        if notice.buyer is None:
            notice.buyer = next((v for v in map(_clean_name, _values(lines, "buyer")) if v), None)
        if notice.org_number is None:
            for value in _values(lines, "org_number"):
                if m := _ORG.search(value):
                    notice.org_number = m.group(1) + m.group(2)
                    break
        if notice.reference is None:
            for value in _values(lines, "reference"):
                ref = " ".join(value.split())[:80].strip(" :-–—,;")
                if re.search(r"\d", ref) and not parse_datetime(ref) and not _is_label(fold(ref)):
                    notice.reference = ref.split("  ")[0]
                    break
        if notice.published is None:
            stamp = next((d for d in map(parse_datetime, _values(lines, "published")) if d), None)
            notice.published = stamp.date() if stamp else None
        if notice.deadline is None:
            notice.deadline = next((d for d in map(parse_datetime, _values(lines, "deadline")) if d), None)
        if notice.procedure is None:
            for value in [*_values(lines, "procedure"), *lines]:
                folded = fold(value)
                kind = next((k for phrase, k in PROCEDURES.items() if phrase in folded), None)
                if kind:
                    notice.procedure = kind
                    break
        if not notice.cpv:
            codes = []
            for i, line in enumerate(lines):
                near = " ".join(lines[max(0, i - 1) : i + 2])
                if "cpv" in fold(near).split() or re.search(r"\d{8}-\d", line):
                    codes += [c for c in _CPV.findall(line) if 3 <= int(c[:2]) <= 98]
            notice.cpv = list(dict.fromkeys(codes))
        notice.framework = notice.framework or any("ramavtal" in fold(line).split() for line in lines[:2000])
    if notice.org_number is None:  # a public body's number anywhere on the best page: they start with 2
        for parser, _ in ranked[:1]:
            for line in parser.lines:
                if (m := _ORG.search(line)) and m.group(1).startswith("2"):
                    notice.org_number = m.group(1) + m.group(2)
                    break
    return notice


def _members(zf: zipfile.ZipFile, prefix: tuple[str, ...], depth: int) -> list[tuple[tuple[str, ...], bytes]]:
    """Candidate notice pages in a zip, and in zips inside it, as (path parts, bytes)."""
    out = []
    for info in zf.infolist():
        if info.is_dir() or info.flag_bits & 0x1:
            continue
        parts = (*prefix, *(p for p in info.filename.replace("\\", "/").split("/") if p))
        suffix = PurePosixPath(info.filename).suffix.casefold()
        try:
            if suffix in NOTICE_SUFFIXES and info.file_size <= MAX_NOTICE_BYTES:
                out.append((parts, zf.read(info)))
            elif suffix == ".zip" and depth < 3 and info.file_size <= MAX_INNER_ZIP_BYTES:
                with zipfile.ZipFile(io.BytesIO(zf.read(info))) as inner:
                    out += _members(inner, parts, depth + 1)
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, OSError, ValueError):
            continue
    return out


def from_zip(path: str | object) -> Notice:
    """Read the notice from a delivered zip without unpacking it to disk. A damaged zip gives an empty notice."""
    try:
        with zipfile.ZipFile(path) as zf:  # type: ignore[arg-type]
            members = _members(zf, (), 0)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, OSError, ValueError):
        return Notice()
    return from_pages(members)


def from_pages(members: list[tuple[tuple[str, ...], bytes]]) -> Notice:
    pages = []
    answer_pages = set()
    for parts, data in members:
        page = read_page(data, parts[-1])
        if page is None:
            continue
        parser, _ = page
        title = " ".join("".join(parser.title).split())
        if (
            classify_path(parts)
            or (title and classify(title))
            or (parser.first_heading and classify(parser.first_heading))
        ):
            answer_pages.add("/".join(parts))  # a page of questions and answers is an answer key, not the notice
            continue
        pages.append(page)
    notice = extract(pages)
    notice.answer_pages = answer_pages
    return notice
