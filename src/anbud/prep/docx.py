"""Word files, from the XML: paragraphs, headings by style or outline level, and table rows.

Deleted text in tracked changes is left out; inserted text is kept. Headers, footers and comments are not read yet.
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


def read_docx(data: bytes, builder: DocBuilder) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        root = ElementTree.fromstring(zf.read("word/document.xml"))
    body = root.find(f"{W}body")
    if body is None:
        return
    line = 0
    for el in body:
        if el.tag == f"{W}p":
            line += 1
            text = _text(el)
            level = _level(el)
            if level is not None:
                number, _ = heading_number(text)
                builder.add(Piece("heading", text, line=line, heading_level=level, heading_number=number))
            else:
                builder.add(Piece("paragraph", text, line=line))
        elif el.tag == f"{W}tbl":
            for tr in el.findall(f"{W}tr"):  # a nested table's text stays inside its cell
                line += 1
                cells = [" ".join(_text(tc).split()) for tc in tr.findall(f"{W}tc")]
                builder.add(Piece("table_row", " | ".join(c for c in cells if c), line=line))
