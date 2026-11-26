"""The document-file guard: PDFs, spreadsheets, drawings, models, images and archives live only in fixtures/synthetic/.

It checks the file extension and the first bytes, so a renamed PDF is caught too.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

ALLOWED_PREFIX = "fixtures/synthetic/"

BLOCKED_EXTENSIONS = {
    # documents and spreadsheets
    ".pdf", ".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx", ".xlsm", ".xlsb", ".ods",
    # drawings and models
    ".dwg", ".dxf", ".ifc", ".ifczip",
    # images
    ".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".bmp", ".heic", ".heif", ".webp", ".svg",
    # Apple iWork, XPS, signed and web archives
    ".pages", ".numbers", ".key", ".xps", ".oxps", ".mht", ".mhtml", ".p7m", ".p7s", ".ifcxml",
    # calc exports
    ".csv", ".tsv",
    # archives, databases, media and mail, which carry the above
    ".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".db", ".sqlite", ".sqlite3", ".parquet",
    ".mp4", ".mov", ".jp2", ".msg", ".eml",
}  # fmt: skip

_MAGIC = (
    (b"%PDF-", "PDF"),
    (b"PK\x03\x04", "zip container (XLSX, DOCX, IFCZIP or ZIP)"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "OLE document (XLS, DOC or MSG)"),
    (b"AC10", "DWG"),
    (b"ISO-10303-21", "IFC/STEP"),
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"\xff\xd8\xff", "JPEG"),
    (b"GIF87a", "GIF"),
    (b"GIF89a", "GIF"),
    (b"II*\x00", "TIFF"),
    (b"MM\x00*", "TIFF"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"Rar!\x1a\x07", "RAR"),
    (b"{\\rtf", "RTF"),
    (b"\x1f\x8b", "gzip"),
    (b"BZh", "bzip2"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"\x28\xb5\x2f\xfd", "zstd"),
    (b"SQLite format 3\x00", "SQLite"),
)
_DXF = re.compile(rb"\A\s*0\r?\n\s*SECTION")


def check(path: str, data: bytes) -> str | None:
    """Return a reason if this file is a document outside fixtures/synthetic/, else None."""
    if path.startswith(ALLOWED_PREFIX):
        return None
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in BLOCKED_EXTENSIONS:
        return f"document file type {suffix}"
    head = data[:64]
    for magic, label in _MAGIC:
        if head.startswith(magic):
            return f"{label} content"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WEBP content"
    if head[4:8] == b"ftyp" and head[8:12] in {b"heic", b"heix", b"mif1", b"msf1", b"avif"}:
        return "HEIC/AVIF content"
    if head.startswith(b"BM") and b"\x00" in data[:1024]:
        return "BMP content"
    if _DXF.match(head):
        return "DXF content"
    if data[257:262] == b"ustar":
        return "tar content"
    return None
