"""Word files, from the XML: paragraphs, headings by style or outline level, and table rows.

- Content controls (`w:sdt`) and custom XML wrappers are read through at body, row and cell level, so a form-like
  tender keeps its text.
- Deleted text in tracked changes is left out; inserted text is kept.
- Headers, footers and footnotes become paragraph blocks after the body, each distinct text once. Comments are not
  read.
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree.ElementTree import Element

from defusedxml import ElementTree

from anbud.prep.build import DocBuilder, Piece
from anbud.prep.text import heading_number

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_HEADING_STYLE = re.compile(r"^(?:heading|rubrik)\s*(\d)$", re.IGNORECASE)


def _text(el: Element) -> str:
    parts = []
    for node in el.iter():
        if node.tag == f"{W}p" and node is not el:
            parts.append(" ")  # paragraphs inside a table cell
        elif node.tag == f"{W}t":
            parts.append(node.text or "")
        elif node.tag in (f"{W}tab", f"{W}br", f"{W}cr"):
            parts.append(" ")
    return "".join(parts)


def _level(p: Element) -> int | None:
    ppr = p.find(f"{W}pPr")
    if ppr is None:
        return None
    outline = ppr.find(f"{W}outlineLvl")
    if outline is not None and (val := outline.get(f"{W}val", "")).isdigit() and int(val) < 9:
        return int(val) + 1
    style = ppr.find(f"{W}pStyle")
    if style is not None and (m := _HEADING_STYLE.match(style.get(f"{W}val", ""))):
        return int(m.group(1))
    return None


_WRAPPERS = (f"{W}sdt", f"{W}sdtContent", f"{W}customXml")
_PARTS = re.compile(r"^word/(?:header\d*|footer\d*|footnotes|endnotes)\.xml$")
_PART_ORDER = ("header", "footer", "footnotes", "endnotes")


def _stem(name: str) -> str:
    return name.removeprefix("word/").removesuffix(".xml").rstrip("0123456789")


def _children(el: Element, tag: str) -> list[Element]:
    """Direct children with `tag`, looking through content controls and custom XML wrappers."""
    out = []
    for child in el:
        if child.tag == tag:
            out.append(child)
        elif child.tag in _WRAPPERS:
            out += _children(child, tag)
    return out


def _flow(el: Element) -> list[Element]:
    """Paragraphs and tables in reading order, looking through wrappers."""
    out = []
    for child in el:
        if child.tag in (f"{W}p", f"{W}tbl"):
            out.append(child)
        elif child.tag in _WRAPPERS:
            out += _flow(child)
    return out


def _add_flow(container: Element, builder: DocBuilder, line: int, headings: bool = True) -> int:
    for el in _flow(container):
        if el.tag == f"{W}p":
            line += 1
            text = _text(el)
            level = _level(el) if headings else None
            if level is not None:
                number, _ = heading_number(text)
                builder.add(Piece("heading", text, line=line, heading_level=level, heading_number=number))
            else:
                builder.add(Piece("paragraph", text, line=line))
        else:
            for tr in _children(el, f"{W}tr"):  # a nested table's text stays inside its cell
                line += 1
                cells = [" ".join(_text(tc).split()) for tc in _children(tr, f"{W}tc")]
                builder.add(Piece("table_row", " | ".join(c for c in cells if c), line=line))
    return line


def read_docx(data: bytes, builder: DocBuilder) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        root = ElementTree.fromstring(zf.read("word/document.xml"))
        names = sorted((n for n in zf.namelist() if _PARTS.match(n)), key=lambda n: (_PART_ORDER.index(_stem(n)), n))
        extras = [ElementTree.fromstring(zf.read(n)) for n in names]
    body = root.find(f"{W}body")
    line = _add_flow(body, builder, 0) if body is not None else 0
    seen: set[str] = set()
    for part in extras:
        for container in [part, *part.findall(f"{W}footnote"), *part.findall(f"{W}endnote")]:
            fresh = [el for el in _flow(container) if " ".join(_text(el).split()) not in seen]
            seen.update(" ".join(_text(el).split()) for el in fresh)
            if fresh:
                wrapper = Element("wrapper")
                wrapper.extend(fresh)
                line = _add_flow(wrapper, builder, line, headings=False)
