from __future__ import annotations

import pytest

from anbud.guards import documents

# Tiny made-up byte strings with the right magic numbers; no real document is used.
SAMPLES = {
    "pdf": b"%PDF-1.7\n%synthetic\n",
    "zip": b"PK\x03\x04" + b"\x00" * 26,
    "ole": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 8,
    "dwg": b"AC1032" + b"\x00" * 8,
    "ifc": b"ISO-10303-21;\nHEADER;\n",
    "png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 8,
    "jpeg": b"\xff\xd8\xff\xe0" + b"\x00" * 8,
    "webp": b"RIFF\x00\x00\x00\x00WEBPVP8 ",
    "heic": b"\x00\x00\x00\x18ftypheic\x00\x00",
    "dxf": b"  0\nSECTION\n  2\nHEADER\n",
}


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_content_is_caught_even_when_renamed(name: str) -> None:
    assert documents.check(f"notes/{name}.txt", SAMPLES[name])


@pytest.mark.parametrize("path", ["a/b/plan.PDF", "calc.xlsx", "ritning.dwg", "modell.ifc", "foto.jpeg", "x.zip"])
def test_extension_is_caught(path: str) -> None:
    assert documents.check(path, b"plain text")


def test_synthetic_fixtures_are_allowed() -> None:
    assert documents.check("fixtures/synthetic/tender-a/plan.pdf", SAMPLES["pdf"]) is None


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("src/anbud/x.py", b"print('hi')\n"),
        ("README.md", b"# Title\n"),
        ("data/notes.txt", b"BM is a fine start for a text line\n"),  # BMP magic, but text
        ("uv.lock", b"version = 1\n"),
    ],
)
def test_ordinary_files_pass(path: str, data: bytes) -> None:
    assert documents.check(path, data) is None
