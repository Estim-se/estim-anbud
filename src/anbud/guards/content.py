"""Turn a file's bytes into the texts the guards scan, or say that it can't be read (then the guards fail closed).

- UTF-8 (with or without a BOM), and UTF-16 and UTF-32 with a BOM or (UTF-16) without, are decoded. Text that isn't
  valid UTF-8 is also read as cp1252, Mac Roman and cp850, so a name with å/ä/ö survives Excel and Windows exports.
- A NUL byte anywhere, or a block with more than a few control characters, means binary, unless the whole file reads
  as UTF-16. UTF-16/32 must be mostly Latin script: random bytes decode there as letters, not controls.
- Each text is also scanned with escapes decoded: \\uXXXX, runs of \\xXX or octal \\NNN read as UTF-8 bytes (Python
  bytes reprs, git's quoted paths), %XX as UTF-8 and as Latin-1, HTML entities, and UTF-8 mojibake ("PÃ¥") repaired.
- Other binary files can't be read. Outside fixtures/synthetic/ that is a finding. Inside it, zip containers (XLSX,
  DOCX) are opened and their text scanned, failing closed on anything odd; PDFs are scanned but also need review.
"""

from __future__ import annotations

import html
import io
import re
import urllib.parse
import zipfile
import zlib

MAX_MEMBER_BYTES = 20_000_000
MAX_TOTAL_BYTES = 100_000_000
_CODE_ESCAPE = re.compile(r"\\u\{([0-9a-fA-F]{1,6})\}|\\u([0-9a-fA-F]{4})|\\U([0-9a-fA-F]{8})")
_BYTE_RUN = re.compile(r"(?:\\x[0-9a-fA-F]{2}|\\[0-3][0-7]{2})+")
_BYTE_PART = re.compile(r"\\x([0-9a-fA-F]{2})|\\([0-3][0-7]{2})")
_TAG = re.compile(r"<[^<>]{0,2000}>")
_PDF_STRING = re.compile(rb"\((?:\\.|[^\\()]){0,4000}\)")
_PDF_HEX = re.compile(rb"<([0-9A-Fa-f\s]{2,8000})>")
_CONTROL = re.compile(r"[\x00-\x08\x0e-\x1f\x7f-\x9f]")  # C0 and C1 controls, except tab, newlines and form feed
_NON_LATIN = re.compile(r"[^\x00-\u024f\u2000-\u206f\u20ac\ufeff]")
_BLOCK = 4096
# A cp1252/Latin-1 reading of a UTF-8 lead byte (Â-ô) followed by readings of continuation bytes.
_CONTINUATION = "\x80-\xbf\u20ac\u201a\u0192\u201e\u2026\u2020\u2021\u02c6\u2030\u0160\u2039\u0152\u017d" + (
    "\u2018\u2019\u201c\u201d\u2022\u2013\u2014\u02dc\u2122\u0161\u203a\u0153\u017e\u0178"
)
_MOJIBAKE = re.compile(f"[\xc2-\xf4][{_CONTINUATION}]+")
MAX_PDF_TEXT = 20_000_000


def _utf16_guess(data: bytes) -> str | None:
    head = data[:4096]
    if len(head) < 4:
        return None
    even = head[0::2].count(0) / max(1, len(head[0::2]))
    odd = head[1::2].count(0) / max(1, len(head[1::2]))
    if odd > 0.3 and even < 0.05:
        return "utf-16-le"
    if even > 0.3 and odd < 0.05:
        return "utf-16-be"
    return None


def _is_text(text: str, latin: bool = False) -> bool:
    """No block holds more than 0.5% control characters (block by block, so a binary tail can't hide behind a text
    head). UTF-16/32 must also be at least 95% Latin script and punctuation; other scripts fail closed."""
    for start in range(0, len(text), _BLOCK):
        block = text[start : start + _BLOCK]
        if len(_CONTROL.findall(block)) > max(2, len(block) // 200):
            return False
        if latin and len(_NON_LATIN.findall(block)) > max(4, len(block) // 20):
            return False
    return True


def decode_text(data: bytes) -> list[str] | None:
    """The decodings to scan, or None if the bytes are binary (the caller then fails closed)."""
    for bom, codec in (
        (b"\xff\xfe\x00\x00", "utf-32-le"),
        (b"\x00\x00\xfe\xff", "utf-32-be"),
        (b"\xff\xfe", "utf-16-le"),
        (b"\xfe\xff", "utf-16-be"),
    ):
        if data.startswith(bom):
            text = data.decode(codec, "replace")
            return [text] if _is_text(text, latin=True) else None
    if b"\0" in data:
        codec = _utf16_guess(data)
        if codec is None:
            return None
        text = data.decode(codec, "replace")
        return [text] if _is_text(text, latin=True) else None
    data = data.removeprefix(b"\xef\xbb\xbf")
    try:
        text = data.decode("utf-8")
        return [text] if _is_text(text) else None
    except UnicodeDecodeError:
        pass
    texts = [data.decode(codec, "replace") for codec in ("utf-8", "cp1252", "mac_roman", "cp850")]
    return texts if _is_text(texts[1]) else None


def _unescape_codes(text: str) -> str:
    def code(match: re.Match[str]) -> str:
        value = int(next(g for g in match.groups() if g), 16)
        return chr(value) if value < 0x110000 and not 0xD800 <= value < 0xE000 else match.group()

    def byte_run(match: re.Match[str]) -> str:
        raw = bytes(int(h, 16) if h else int(o, 8) for h, o in _BYTE_PART.findall(match.group()))
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("cp1252", "replace")

    return _BYTE_RUN.sub(byte_run, _CODE_ESCAPE.sub(code, text))


def _repair_run(match: re.Match[str]) -> str:
    for codec in ("cp1252", "latin-1"):
        try:
            return match.group().encode(codec).decode("utf-8")
        except UnicodeError:
            continue
    return match.group()


def _repair_mojibake(text: str) -> str:
    """UTF-8 read as cp1252 or Latin-1 and saved again ('PÃ¥hittade') turned back into 'Påhittade', run by run, so
    correct å/ä/ö or emoji elsewhere in the text don't stop it."""
    return _MOJIBAKE.sub(_repair_run, text) if "Ã" in text or "Â" in text else text


def variants(text: str) -> list[str]:
    """The text plus its unescaped and repaired forms, without duplicates."""
    step = _unescape_codes(text)
    unescaped = [
        text,
        html.unescape(urllib.parse.unquote(step, errors="replace")),
        html.unescape(urllib.parse.unquote(step, encoding="latin-1")),
    ]
    return list(dict.fromkeys([*unescaped, *map(_repair_mojibake, unescaped)]))


def path_texts(raw: bytes) -> list[str]:
    """A path's readings: UTF-8, or cp1252/Mac Roman/cp850 for bytes that aren't (unzipped Windows archives)."""
    try:
        texts = [raw.decode("utf-8")]
    except UnicodeDecodeError:
        texts = [raw.decode(codec, "replace") for codec in ("cp1252", "mac_roman", "cp850")]
    return [v for t in texts for v in variants(t)]


def _inflate(body: bytes, budget: list[int]) -> bytes | None:
    if budget[0] <= 0:  # decompress(..., 0) would mean "no limit"
        return None
    try:
        out = zlib.decompressobj().decompress(body, budget[0] + 1)
    except zlib.error:
        return None
    budget[0] -= len(out)
    return None if budget[0] < 0 else out


def _pdf_streams(data: bytes) -> list[bytes]:
    """The bodies between `stream` and `endstream`, found in one linear pass."""
    bodies = []
    pos = 0
    while True:
        start = data.find(b"stream", pos)
        if start < 0:
            break
        body_start = start + len(b"stream")
        if data[body_start : body_start + 2] == b"\r\n":
            body_start += 2
        elif data[body_start : body_start + 1] in (b"\n", b"\r"):
            body_start += 1
        else:
            pos = body_start
            continue
        end = data.find(b"endstream", body_start)
        if end < 0:
            break
        bodies.append(data[body_start:end].rstrip(b"\r\n"))
        pos = end + len(b"endstream")
    return bodies


def _pdf_string(raw: bytes) -> str:
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", "replace")
    return raw.decode("latin-1")


def _pdf_texts(data: bytes) -> list[str] | None:
    """Text from a PDF's strings, plus the raw and inflated streams that read as text (fonts and images don't, and
    scanning them would take minutes). None past the text budget: the PDF then needs review only."""
    budget = [MAX_TOTAL_BYTES]
    chunks = [data]
    for body in _pdf_streams(data):
        inflated = _inflate(body, budget)
        chunks.append(inflated if inflated is not None else body)
    texts: list[str] = []
    for chunk in chunks:
        raw_text = chunk.decode("latin-1")
        if _is_text(raw_text):
            texts.append(raw_text)
        strings = [_unescape_pdf(s[1:-1]) for s in _PDF_STRING.findall(chunk)]
        for hex_string in _PDF_HEX.findall(chunk):
            digits = re.sub(rb"\s", b"", hex_string)
            if len(digits) % 2 == 0:
                strings.append(bytes.fromhex(digits.decode()))
        if strings:
            decoded = [t for t in map(_pdf_string, strings) if _is_text(t)]  # random bytes in images aren't text
            texts += ["".join(decoded), " ".join(decoded)]
        if sum(map(len, texts)) > MAX_PDF_TEXT:
            return None
    return texts


def _unescape_pdf(raw: bytes) -> bytes:
    """PDF literal-string escapes: \\ooo octal, \\n and friends, escaped parentheses."""
    simple = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}

    def one(match: re.Match[bytes]) -> bytes:
        token = match.group(1)
        if re.fullmatch(rb"[0-7]{1,3}", token):
            return bytes([int(token, 8) & 0xFF])
        return simple.get(token, token)

    return re.sub(rb"\\([0-7]{1,3}|.)", one, raw, flags=re.S)


def _zip_texts(data: bytes) -> list[tuple[str, str]] | None:
    """Member names, the archive comment and each member's text. None if anything can't be read: an encrypted, binary,
    oversize or corrupt member, data on a directory entry, or a total over the budget. Never raises."""
    out: list[tuple[str, str]] = []
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            out.append(("comment", archive.comment.decode("utf-8", "replace")))
            for info in archive.infolist():
                out.append((info.filename, info.filename))
                if info.flag_bits & 0x1 or info.file_size > MAX_MEMBER_BYTES:
                    return None
                total += info.file_size
                if total > MAX_TOTAL_BYTES:
                    return None
                member = archive.read(info)
                if info.is_dir():
                    if member:
                        return None
                    continue
                decoded = decode_text(member)
                if decoded is None:
                    return None
                for text in decoded:
                    out.append((info.filename, text))
                    if "<" in text:
                        out.append((info.filename, _TAG.sub("", text)))
                        out.append((info.filename, _TAG.sub(" ", text)))
    except Exception:  # noqa: BLE001 - any failure means the container can't be read; messages may hold names
        return None
    return out


def fixture_texts(data: bytes) -> list[tuple[str, str]] | None:
    """(member, text) pairs from a fixture container, or None if it can't be read."""
    if data.startswith(b"PK\x03\x04"):
        return _zip_texts(data)
    if data.startswith(b"%PDF-"):
        try:
            texts = _pdf_texts(data)
        except Exception:  # noqa: BLE001 - see above
            return None
        return None if texts is None else [("pdf", t) for t in texts]
    return None
