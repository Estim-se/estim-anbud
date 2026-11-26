"""Synthetic documents for the prep tests, built in memory: a minimal PDF writer, a DOCX writer and workbook helpers.

Every text is invented. Nothing here is written to the repo; tests put the files in temp folders.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field

A4 = (595.0, 842.0)
A3_LANDSCAPE = (1191.0, 842.0)
A2 = (1191.0, 1684.0)


@dataclass
class Text:
    x: float
    top: float  # distance from the top of the page to the top of the text, in points
    text: str
    size: float = 10.0
    bold: bool = False


@dataclass
class Page:
    size: tuple[float, float] = A4
    texts: list[Text] = field(default_factory=list)
    image: tuple[int, int] | None = None  # pixel size of a gray image stretched over the whole page
    vector_lines: int = 0
    rotate: int = 0  # 0 or 90; with 90, `size` is the stored (portrait) page and text positions are as displayed


def _escape(text: str) -> bytes:
    raw = text.encode("cp1252")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def make_pdf(pages: list[Page]) -> bytes:
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # filled in at the end
    pages_obj = add(b"")
    regular = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    bold = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    kids = []
    for page in pages:
        width, height = page.size
        ops = []
        resources = [f"/Font << /F1 {regular} 0 R /F2 {bold} 0 R >>".encode()]
        if page.image:
            w, h = page.image
            pixels = bytes((x * 255 // max(w - 1, 1) + y) % 256 for y in range(h) for x in range(w))
            img = add(
                f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace /DeviceGray "
                f"/BitsPerComponent 8 /Length {len(pixels)} >>\nstream\n".encode()
                + pixels
                + b"\nendstream"
            )
            resources.append(f"/XObject << /Im1 {img} 0 R >>".encode())
            ops.append(f"q {width} 0 0 {height} 0 0 cm /Im1 Do Q".encode())
        for i in range(page.vector_lines):
            x = 20 + (i * 37) % (width - 40)
            ops.append(f"0.5 w {x:.1f} 20 m {width - x:.1f} {height - 20} l S".encode())
        for t in page.texts:
            font = "/F2" if t.bold else "/F1"
            if page.rotate == 90:  # displayed (x, baseline) maps to stored (u, v) = (baseline, x), text running up
                u = t.top + t.size * 0.8
                matrix = f"0 1 -1 0 {u:.2f} {t.x}"
            else:
                matrix = f"1 0 0 1 {t.x} {height - t.top - t.size * 0.8:.2f}"
            ops.append(f"BT {font} {t.size} Tf {matrix} Tm (".encode() + _escape(t.text) + b") Tj ET")
        content = b"\n".join(ops)
        stream = add(f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream")
        kid = add(
            f"<< /Type /Page /Parent {pages_obj} 0 R /MediaBox [0 0 {width} {height}] /Rotate {page.rotate} "
            f"/Contents {stream} 0 R "
            f"/Resources << ".encode()
            + b" ".join(resources)
            + b" >> >>"
        )
        kids.append(kid)
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode()
    objects[pages_obj - 1] = (
        f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
    )
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{n} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _p(text: str, style: str | None = None, deleted: str | None = None) -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    gone = f"<w:del><w:r><w:delText>{deleted}</w:delText></w:r></w:del>" if deleted else ""
    return f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r>{gone}</w:p>"


def _tc(inner: str) -> str:
    return f"<w:tc>{inner}</w:tc>"


def make_docx(body: str, parts: dict[str, str] | None = None) -> bytes:
    """`parts` maps extra part names (word/header1.xml, word/footnotes.xml) to their root element's inner XML; the
    root tag follows from the name."""
    roots = {"header": "w:hdr", "footer": "w:ftr", "footnotes": "w:footnotes", "endnotes": "w:endnotes"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", f"<w:document {W}><w:body>{body}</w:body></w:document>")
        for name, inner in (parts or {}).items():
            stem = name.rsplit("/", 1)[-1].split(".")[0].rstrip("0123456789")
            tag = roots[stem]
            zf.writestr(name, f"<{tag} {W}>{inner}</{tag}>")
    return buf.getvalue()


def docx_paragraph(text: str, style: str | None = None, deleted: str | None = None) -> str:
    return _p(text, style, deleted)


def docx_table(rows: list[list[str]]) -> str:
    return "<w:tbl>" + "".join("<w:tr>" + "".join(_tc(c) for c in row) + "</w:tr>" for row in rows) + "</w:tbl>"


def luhn_digit(nine: str) -> str:
    total = 0
    for i, ch in enumerate(nine):
        d = int(ch) * (2 if i % 2 == 0 else 1)
        total += d - 9 if d > 9 else d
    return str((10 - total % 10) % 10)


def personnummer(date6: str, serial3: str = "123") -> str:
    """A checksum-valid, invented personnummer such as 811218-1238."""
    return f"{date6}-{serial3}{luhn_digit(date6 + serial3)}"
