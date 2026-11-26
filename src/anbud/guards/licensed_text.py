"""The licensed-text guard.

CI can't compare against texts it doesn't have, so it flags the shapes that pasted text takes:
- a quoted passage or blockquote of at least QUOTE_WORDS words, in prose, lens and prompt files;
- a paragraph of at least PARAGRAPH_WORDS words that names a licensed source, in any text file.
Lens checklists cite clause or code numbers with short Estim paraphrases, so neither shape should appear.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

QUOTE_WORDS = 30
PARAGRAPH_WORDS = 60

PROSE_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".adoc"}
PROSE_DIRS = ("lenses/", "prompts/", "references/", "docs/")
SKIP_PREFIXES = ("tests/", "fixtures/synthetic/")
SKIP_NAMES = {"uv.lock"}

MARKERS = re.compile(
    r"\bAMA\b|\bMER\b|\bRA[   ](?:Bygg|Anl|Anläggning|Hus|VVS|EL)\b|\bRA[   -]?\d{2}\b"
    r"|\bABT?(?:-U)?[   -]?(?:0[4-9]|[1-9]\d)\b|\bSIS\b|\bSS(?:-EN)?[   -]?\d{3}"
    r"|\bSEK[   ](?:Handbok|TK)\b|\bSvensk Elstandard\b"
    r"|\bATL\b|\bByggjura\b|\bSvensk Byggtjänst\b"
)
_WORD = re.compile(r"[^\W\d_]+")
_QUOTES = (
    re.compile(r"[“„]([^“”„]+?)[”“]", re.S),
    re.compile(r"«([^«»]+?)»", re.S),
    re.compile(r"»([^«»]+?)«", re.S),
    re.compile(r'"([^"]+?)"', re.S),
)


def _words(text: str) -> int:
    return len(_WORD.findall(text))


def _paragraphs(text: str) -> list[tuple[int, str]]:
    out, start, buf = [], 0, []
    for number, line in enumerate(text.splitlines(), start=1):
        if line.strip():
            if not buf:
                start = number
            buf.append(line)
        elif buf:
            out.append((start, "\n".join(buf)))
            buf = []
    if buf:
        out.append((start, "\n".join(buf)))
    return out


def in_scope(path: str) -> bool:
    return not path.startswith(SKIP_PREFIXES) and PurePosixPath(path).name not in SKIP_NAMES


def is_prose(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in PROSE_SUFFIXES or path.startswith(PROSE_DIRS)


def check(path: str, text: str) -> list[tuple[int, str]]:
    """Return (line, reason) pairs. Reasons never quote the text."""
    if not in_scope(path):
        return []
    findings: list[tuple[int, str]] = []
    prose = is_prose(path)
    for start, para in _paragraphs(text):
        if prose:
            lines = para.splitlines()
            quoted = [ln.lstrip()[1:] for ln in lines if ln.lstrip().startswith(">")]
            if quoted and _words(" ".join(quoted)) >= QUOTE_WORDS:
                findings.append((start, f"blockquote of {QUOTE_WORDS}+ words"))
            for pattern in _QUOTES:
                for match in pattern.finditer(para):
                    if _words(match.group(1)) >= QUOTE_WORDS:
                        line = start + para.count("\n", 0, match.start())
                        findings.append((line, f"quoted passage of {QUOTE_WORDS}+ words"))
        if _words(para) >= PARAGRAPH_WORDS and MARKERS.search(para):
            findings.append((start, f"passage of {PARAGRAPH_WORDS}+ words next to a licensed-source reference"))
    return sorted(set(findings))
