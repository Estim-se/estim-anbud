"""Saved web pages: the procurement platform's notice page, as HTML, MHTML or a Safari web archive.

- Text becomes blocks like any other document: headings with their levels, paragraphs, and one `table_row` per table
  row with its cells joined by " | ". Scripts, styles, navigation, form controls and embedded objects are dropped.
- A notice page may carry the buyer's questions and answers, amendments or award decision inline, often in a tab. Those
  are the answer key for blind scoring, so they are withheld before any text reaches a block, the outline, masking or
  the security gate. The rule is `anbud.answer_terms.classify`, the same one intake uses for files:
  - a heading that classifies withholds itself and everything after it up to the next heading of the same or a higher
    level, or the end of the section, article or details element it sits in;
  - a `summary` or `legend` that classifies withholds its details or fieldset;
  - an element whose id, class, aria-label, title or data-* value classifies is withheld whole.
  When the page has no h1, its title stands in as one, under the same rule.
- Hidden elements (the hidden attribute, aria-hidden, inline display:none or visibility:hidden) become hidden blocks.
- The parser is event-based and the element stack is an explicit list with a depth cap, so deep or broken markup
  can't exhaust the stack. Elements opened past the cap are counted, not tracked: a close tag then closes one of them
  first, so a withheld element is never closed early by markup nested past the cap.

Layout tables (a whole page inside a table, still common on older platforms) are detected when a cell holds a heading
or another table: from there the row is read as ordinary blocks instead of one long row.
"""

from __future__ import annotations

import codecs
import email
import plistlib
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from anbud.answer_terms import classify
from anbud.prep.build import DocBuilder, Piece
from anbud.prep.text import heading_number

MAX_DEPTH = 1024
MAX_ATTR_CHARS = 4096  # of one attribute value, classified; a longer value is data, not a label

_SKIP = frozenset(
    "script style noscript template svg math head title nav button select option input textarea iframe object embed "
    "canvas".split()
)
_VOID = frozenset("area base br col embed hr img input link meta param source track wbr keygen".split())
_HEADINGS = {f"h{n}": n for n in range(1, 7)}
_LABELS = frozenset(("summary", "legend"))
_SECTIONS = frozenset(("section", "article", "details"))
_BLOCK = frozenset(
    "address article aside blockquote body caption center dd details dialog dir div dl dt fieldset figcaption figure "
    "footer form h1 h2 h3 h4 h5 h6 header hgroup hr html label legend li main menu ol p pre section summary table "
    "tbody td tfoot th thead tr ul".split()
)
# Start tag -> (open elements it closes, elements that stop the search). "<li>a<li>b" is two items, not nested ones.
_IMPLIED: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "li": (frozenset(("li",)), frozenset(("ul", "ol", "menu"))),
    "dt": (frozenset(("dt", "dd")), frozenset(("dl",))),
    "dd": (frozenset(("dt", "dd")), frozenset(("dl",))),
    "tr": (frozenset(("tr",)), frozenset(("table",))),
    "td": (frozenset(("td", "th")), frozenset(("tr", "table"))),
    "th": (frozenset(("td", "th")), frozenset(("tr", "table"))),
    "thead": (frozenset(("thead", "tbody", "tfoot")), frozenset(("table",))),
    "tbody": (frozenset(("thead", "tbody", "tfoot")), frozenset(("table",))),
    "tfoot": (frozenset(("thead", "tbody", "tfoot")), frozenset(("table",))),
}
_P = frozenset(("p",))
_P_AND_HEADINGS = frozenset(("p", *_HEADINGS))
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)
_LABEL_ATTRS = frozenset(("id", "class", "aria-label", "title"))

_META_CHARSET = re.compile(rb"<meta[^>]*?charset\s*=\s*[\"']?\s*([A-Za-z0-9_.:-]{1,40})", re.IGNORECASE)
_XML_ENCODING = re.compile(rb"<\?xml[^>]*?encoding\s*=\s*[\"']([A-Za-z0-9_.:-]{1,40})", re.IGNORECASE)
_WINDOWS_1252 = frozenset(("iso-8859-1", "iso8859-1", "latin1", "latin-1", "l1", "us-ascii", "ascii", "windows-1252"))
_HEADER_LINE = re.compile(rb"^[A-Za-z][A-Za-z0-9-]{0,76}:")


def looks_like_mime(head: bytes) -> bool:
    """A MIME message head (MHTML): header lines first, and a content type among them."""
    return bool(_HEADER_LINE.match(head.lstrip())) and b"content-type:" in head.lower()


def _codec(name: str | bytes | None) -> str | None:
    """A Python codec for a declared charset, or None if it's unknown. Latin-1 and ASCII mean Windows-1252, as in
    browsers, and a UTF-16 declaration without a byte order mark means UTF-8."""
    if not name:
        return None
    label = (name.decode("ascii", "ignore") if isinstance(name, bytes) else name).strip().lower()
    if label in _WINDOWS_1252:
        return "cp1252"
    if label.startswith(("utf-16", "utf-32", "ucs-2")):
        return "utf-8"
    try:
        return codecs.lookup(label).name
    except LookupError:
        return None


def decode(raw: bytes, declared: str | None = None) -> str:
    """Byte order mark, then the container's charset, then the page's own declaration, else UTF-8 with a Windows-1252
    fallback."""
    if raw.startswith(codecs.BOM_UTF8):
        return raw[3:].decode("utf-8", errors="replace")
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16", errors="replace")
    head = raw[:4096]
    found = _META_CHARSET.search(head) or _XML_ENCODING.search(head)
    codec = _codec(declared) or _codec(found.group(1) if found else None)
    if codec:
        try:
            return raw.decode(codec, errors="replace")
        except LookupError:  # a codec that isn't a text encoding
            pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _mhtml_pages(data: bytes) -> list[str]:
    """Every text/html part, in order: the page first, then its frames."""
    message = email.message_from_bytes(data)
    pages = []
    for part in message.walk():
        if part.get_content_type() != "text/html":
            continue
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            pages.append(decode(payload, part.get_content_charset()))
    return pages


def _webarchive_page(data: bytes) -> str:
    archive = plistlib.loads(data)
    main = archive.get("WebMainResource") if isinstance(archive, dict) else None
    raw = main.get("WebResourceData") if isinstance(main, dict) else None
    mime = main.get("WebResourceMIMEType", "text/html") if isinstance(main, dict) else None
    if not isinstance(raw, bytes) or not isinstance(mime, str) or "html" not in mime.lower():
        raise ValueError("the web archive holds no web page")
    encoding = main.get("WebResourceTextEncodingName")
    return decode(raw, encoding if isinstance(encoding, str) else None)


@dataclass(eq=False)
class _Row:
    depth: int  # stack length with the row's own element on top
    hidden: bool
    cells: list[list[str]] = field(default_factory=list)
    layout: bool = False

    def text(self) -> str:
        cells = (" ".join("".join(c).split()) for c in self.cells)
        return " | ".join(c for c in cells if c)


@dataclass(eq=False)
class _Frame:
    """An open element. The flags are inherited: a frame inside a hidden element is hidden too."""

    tag: str
    parent: _Frame | None = None
    skip: bool = False
    hidden: bool = False
    withheld: bool = False
    heading: int | None = None
    label: _Frame | None = None  # the summary or legend this text is in
    title: bool = False
    row: _Row | None = None


@dataclass(eq=False)
class _Section:
    """A withheld section started by a heading: ends at a heading of this level or higher, or with its container."""

    level: int
    container: _Frame | None


def _is_hidden(attrs: dict[str, str | None]) -> bool:
    return (
        "hidden" in attrs
        or (attrs.get("aria-hidden") or "").strip().lower() == "true"
        or bool(_HIDDEN_STYLE.search((attrs.get("style") or "")[:MAX_ATTR_CHARS]))
    )


def _labels_answer(attrs: dict[str, str | None]) -> bool:
    return any(
        classify(value[:MAX_ATTR_CHARS])
        for name, value in attrs.items()
        if value and (name in _LABEL_ATTRS or name.startswith("data-"))
    )


class _Page(HTMLParser):
    """One page's pieces, in reading order, with the answer key left out."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Frame("#root")
        self.stack: list[_Frame] = [self.root]
        self.overflow = 0  # elements opened past MAX_DEPTH and not yet closed
        self.rows: list[_Row] = []
        self.section: _Section | None = None
        self.pieces: list[Piece] = []
        self.withheld = 0
        self.title: list[str] = []
        self.has_h1 = False
        self.buf: list[str] = []
        self.buf_frame: _Frame = self.root

    # -- events

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID:
            if tag in ("br", "wbr", "hr"):
                self._text(" ")
            return
        if self.overflow or len(self.stack) > MAX_DEPTH:
            self.overflow += 1
            return
        self._close_implied(tag)
        parent = self.stack[-1]
        if tag == "h1" and not parent.skip:
            self.has_h1 = True
        level = _HEADINGS.get(tag)
        if level is not None and self.section and level <= self.section.level:
            self.section = None
        values = dict(attrs)
        frame = _Frame(
            tag,
            parent,
            skip=parent.skip or tag in _SKIP,
            hidden=parent.hidden or _is_hidden(values),
            withheld=parent.withheld,
            heading=level or parent.heading,
            label=parent.label,
            title=tag == "title" and (parent.tag == "head" or not parent.skip),
        )
        if tag in _LABELS:
            frame.label = frame
        if not frame.withheld and not frame.skip and not self.section and _labels_answer(values):
            frame.withheld = True
            self.withheld += 1
        row = self._active_row()
        if row and (level is not None or tag == "table"):
            self._end_row_cells(row)  # a layout table: read the rest of this row as blocks
        self._boundary(frame)
        self.stack.append(frame)
        row = self._active_row()
        if tag == "tr":
            frame.row = _Row(len(self.stack), frame.hidden)
            self.rows.append(frame.row)
        elif tag in ("td", "th") and row and len(self.stack) == row.depth + 1:
            row.cells.append([])

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        if self.overflow:
            self.overflow -= 1
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                self._pop_to(i)
                return

    def handle_data(self, data: str) -> None:
        self._text(data)

    def close(self) -> None:
        super().close()
        self._pop_to(1)
        self._flush()

    # -- structure

    def _close_implied(self, tag: str) -> None:
        """Close what the start tag implicitly ends. Without explicit stops, the search crosses only inline elements:
        a block start closes an open paragraph, a heading start an open heading."""
        if tag in _IMPLIED:
            closes, stops = _IMPLIED[tag]
        elif tag in _BLOCK:
            closes, stops = (_P_AND_HEADINGS if tag in _HEADINGS else _P), _BLOCK
        else:
            return
        for i in range(len(self.stack) - 1, 0, -1):
            name = self.stack[i].tag
            if name in closes:
                self._pop_to(i)
                return
            if name in stops:
                return

    def _pop_to(self, index: int) -> None:
        while len(self.stack) > index:
            frame = self.stack[-1]
            self._boundary(frame)
            self.stack.pop()
            if self.section and self.section.container is frame:
                self.section = None
            if frame.row is not None:
                self._end_row(frame.row)

    def _boundary(self, frame: _Frame) -> None:
        """Entering or leaving a block, or crossing into hidden or withheld content, ends the current text."""
        parent = frame.parent or self.root
        changes = (frame.hidden, frame.withheld, frame.skip) != (parent.hidden, parent.withheld, parent.skip)
        if frame.tag not in _BLOCK and not changes:
            return
        row = self._active_row()
        if row and row.cells:
            row.cells[-1].append(" ")
        else:
            self._flush()

    def _active_row(self) -> _Row | None:
        return self.rows[-1] if self.rows and not self.rows[-1].layout else None

    # -- text

    def _text(self, data: str) -> None:
        top = self.stack[-1]
        if top.title:
            self.title.append(data)
            return
        if top.skip or top.withheld or self.section:
            return
        row = self._active_row()
        if row:
            if not row.cells:
                row.cells.append([])
            row.cells[-1].append(data)
            row.hidden = row.hidden or top.hidden
            return
        if not self.buf:
            self.buf_frame = top
        self.buf.append(data)

    def _flush(self) -> None:
        text = " ".join("".join(self.buf).split())
        frame, self.buf = self.buf_frame, []
        if not text:
            return
        if frame.heading is not None:
            if classify(text):
                self.withheld += 1
                self.section = _Section(frame.heading, self._container(frame))
                return
            number, _ = heading_number(text)
            self.pieces.append(
                Piece("heading", text, hidden=frame.hidden, heading_level=frame.heading, heading_number=number)
            )
        elif frame.label is not None and classify(text):
            self._withhold_labelled(frame.label)
        else:
            self.pieces.append(Piece("paragraph", text, hidden=frame.hidden))

    def _withhold_labelled(self, label: _Frame) -> None:
        """The summary or legend's element (details, fieldset) is withheld from here on; its label is dropped."""
        owner = label.parent or self.root
        if owner is self.root or owner.withheld:
            return
        self.withheld += 1
        for frame in self.stack[self.stack.index(owner) :] if owner in self.stack else []:
            frame.withheld = True

    def _container(self, frame: _Frame) -> _Frame | None:
        node = frame.parent
        while node is not None and node.tag not in _SECTIONS:
            node = node.parent
        return node

    def _end_row_cells(self, row: _Row) -> None:
        if text := row.text():
            self.pieces.append(Piece("table_row", text, hidden=row.hidden))
        row.cells = []
        row.layout = True

    def _end_row(self, row: _Row) -> None:
        if row in self.rows:
            self.rows.remove(row)
        if row.layout:
            self._flush()
        elif text := row.text():
            self.pieces.append(Piece("table_row", text, hidden=row.hidden))

    # -- result

    def result(self) -> list[Piece]:
        """The pieces, with the title as the level-1 heading when the page has none."""
        title = " ".join("".join(self.title).split())
        if self.has_h1 or not title:
            return self.pieces
        if classify(title):  # a level-1 heading with no other level-1 heading after it: the whole page
            self.withheld += 1
            self.pieces = []
            return self.pieces
        number, _ = heading_number(title)
        return [Piece("heading", title, heading_level=1, heading_number=number), *self.pieces]


def read_html(data: bytes, builder: DocBuilder) -> int:
    """Blocks and outline go into the builder; returns how many answer-key sections were withheld."""
    if data.startswith(b"bplist00"):
        pages = [_webarchive_page(data)]
    elif looks_like_mime(data[:4096]):
        pages = _mhtml_pages(data)
    else:
        pages = [decode(data)]
    withheld = 0
    pieces: list[Piece] = []
    for text in pages:
        page = _Page()
        try:
            page.feed(text)
            page.close()
        except AssertionError:  # how the standard parser reports some malformed declarations
            raise ValueError("the page's markup can't be parsed") from None
        pieces += page.result()
        withheld += page.withheld
    for line, piece in enumerate(pieces, 1):
        piece.line = line
        builder.add(piece)
    return withheld
