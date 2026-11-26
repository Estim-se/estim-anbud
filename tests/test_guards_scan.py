"""What the guards read out of a file's bytes. Invented names and synthetic bytes only."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import time
import zipfile
import zlib

import pytest
from conftest import FAKE_NAMES, assert_no_fake_names

from anbud.guards import content, documents, names
from anbud.guards.__main__ import Scanner

NAME_LINE = "Möte med Påhittade Fastigheter i morgon\n"
PERSON_LINE = "Kontakt: Örjan Exempelsson\n"  # Ö is 0xD6 in cp1252 but '÷' in Mac Roman: only cp1252 finds it


def matcher() -> names.Matcher:
    return names.Matcher.from_entries(names.parse_plain(FAKE_NAMES))


def scan(path: str, data: bytes, manifest: set[str] | None = None) -> list:
    scanner = Scanner(matcher(), manifest or set())
    scanner.file(path, data)
    assert_no_fake_names(*(str(f) for f in scanner.findings))
    return scanner.findings


def kinds(findings) -> list[str]:
    return sorted({f.guard for f in findings})


@pytest.mark.parametrize(
    "data",
    [
        NAME_LINE.encode("utf-16"),  # with a BOM
        NAME_LINE.encode("utf-16-le"),  # without one
        NAME_LINE.encode("utf-16-be"),
        NAME_LINE.encode("utf-32"),
        PERSON_LINE.encode("cp1252"),  # the Excel default on Windows
        NAME_LINE.encode("mac_roman"),
        NAME_LINE.encode("cp850"),  # the Swedish Windows console
        b"\xef\xbb\xbf" + b"rubrik\n" + PERSON_LINE.encode("cp1252"),  # a UTF-8 BOM in front of cp1252 rows
        json.dumps({"kund": NAME_LINE}).encode(),  # å escapes
        repr(NAME_LINE.encode()).encode(),  # a Python bytes repr: \xc3\xa5
        b'modified: "M\\303\\266te med P\\303\\245hittade Fastigheter"',  # git's quoted path
        NAME_LINE.replace("å", "&aring;").encode(),  # HTML entity
        NAME_LINE.replace("å", "&#229;").encode(),
        NAME_LINE.replace("å", "%C3%A5").encode(),  # URL escape, UTF-8
        NAME_LINE.replace("å", "%E5").encode(),  # URL escape, Latin-1
        NAME_LINE.encode().decode("cp1252").encode(),  # mojibake: PÃ¥hittade saved as UTF-8
        ("Rätt stavat: åäö ✅\n" + NAME_LINE.encode().decode("cp1252")).encode(),  # mojibake next to correct text
    ],
)
def test_names_are_found_in_any_text_encoding_or_escape(data: bytes) -> None:
    assert kinds(scan("notes/a.txt", data)) == ["names"]


@pytest.mark.parametrize(
    "data",
    [
        gzip.compress(NAME_LINE.encode()),
        b"\x00\x01\x02" * 100,  # an unknown binary
        b"SQLite format 3\x00" + b"\x00" * 100,
        b"x" * 257 + b"ustar\x00" + b"\x00" * 100,  # tar header
        b"ren text\n" * 1000 + PERSON_LINE.encode("utf-16-le"),  # a UTF-16 fragment after 8 KB of UTF-8
        b"ren text\n" * 1000 + zlib.compress(bytes(range(256)) * 2),  # a small compressed tail
        b"\xff\xfe" + bytes(range(256)) * 4,  # a UTF-16 BOM in front of binary
        bytes(b for i in range(2000) for b in (0, i % 31)),  # zeros in every other byte, controls in between
    ],
)
def test_binaries_outside_fixtures_fail_closed(data: bytes) -> None:
    assert kinds(scan("data/blob.txt", data)) == ["documents"]


@pytest.mark.parametrize("path", ["a.tar", "a.tgz", "a.gz", "a.sqlite", "a.csv", "a.numbers", "a.p7m", "a.mp4"])
def test_container_and_export_extensions_are_blocked(path: str) -> None:
    assert documents.check(path, b"plain text")


def _zip(members: dict[str, bytes], comment: bytes = b"") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.comment = comment
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_a_fixture_container_is_opened_and_scanned() -> None:
    assert scan("fixtures/synthetic/a.xlsx", _zip({"xl/sharedStrings.xml": b"<si><t>Exempelrad</t></si>"})) == []
    split_by_tags = _zip({"xl/sharedStrings.xml": "<si><t>Påhittade</t><t> Fastigheter</t></si>".encode()})
    assert kinds(scan("fixtures/synthetic/a.xlsx", split_by_tags)) == ["names"]
    assert kinds(scan("fixtures/synthetic/b.zip", _zip({"Fiktivbolaget AB/plan.txt": b"text"}))) == ["names"]
    assert kinds(scan("fixtures/synthetic/c.zip", _zip({"a.txt": b"text"}, b"Fiktivbolaget AB"))) == ["names"]


def test_unreadable_fixture_containers_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    name = "Fiktivbolaget AB.xml"
    good = _zip({name: b"<x>" + bytes(range(32, 127)) * 20 + b"</x>"})
    corrupt = bytearray(good)
    corrupt[30 + len(name) + 8] ^= 0xFF  # a byte inside the compressed data: bad CRC or a zlib error
    assert kinds(scan("fixtures/synthetic/d.zip", bytes(corrupt))) == ["documents"]  # no crash, no name printed
    monkeypatch.setattr(content, "MAX_MEMBER_BYTES", 100)
    assert "documents" in kinds(scan("fixtures/synthetic/e.zip", good))  # an oversize member


def test_a_fixture_pdf_is_scanned_and_needs_review() -> None:
    def pdf(stream: bytes) -> bytes:
        return b"%PDF-1.7\n1 0 obj\n<< /Filter /FlateDecode >>\nstream\n" + zlib.compress(stream) + b"\nendstream\n"

    kerned = pdf(b"BT [(Fiktiv) -20 (bolaget AB)] TJ ET")  # only the joined strings hold the name
    assert kinds(scan("fixtures/synthetic/a.pdf", kerned)) == ["documents", "names"]
    hex_text = b"BT <" + b"Fiktivbolaget AB".hex().encode() + b"> Tj ET"
    assert "names" in kinds(scan("fixtures/synthetic/b.pdf", pdf(hex_text)))
    clean = pdf(b"BT (Exempelrad) Tj ET")
    assert kinds(scan("fixtures/synthetic/c.pdf", clean)) == ["documents"]  # needs its hash in the manifest
    assert scan("fixtures/synthetic/c.pdf", clean, {hashlib.sha256(clean).hexdigest()}) == []


def test_pdf_parsing_stays_linear_on_hostile_input() -> None:
    start = time.monotonic()
    content.fixture_texts(b"%PDF-1.7\n" + b"(" * 300_000 + b"stream\n" * 20_000)
    assert time.monotonic() - start < 5


def test_a_fixture_binary_needs_its_hash_in_the_manifest() -> None:
    data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    assert kinds(scan("fixtures/synthetic/a.png", data)) == ["documents"]
    assert scan("fixtures/synthetic/a.png", data, {hashlib.sha256(data).hexdigest()}) == []
    unreadable = _zip({"a.png": data})  # a binary member: the container can't be read
    assert kinds(scan("fixtures/synthetic/c.docx", unreadable)) == ["documents"]


def test_names_in_paths_are_found_in_any_encoding_and_never_printed() -> None:
    for raw in ("kunder/Påhittade Fastigheter/x.txt".encode("cp1252"), b"Fiktivbolaget%20AB%20offert.txt"):
        scanner = Scanner(matcher(), set())
        scanner.file(raw.decode("utf-8", "surrogateescape"), b"ren text\n")
        assert kinds(scanner.findings) == ["names"]
        assert all("hidden" in str(f) for f in scanner.findings)


def test_ordinary_text_stays_text() -> None:
    assert content.decode_text(b"vanlig text\tmed tabb\n") == ["vanlig text\tmed tabb\n"]
    assert content.decode_text(b"\x00\x01\x02\x03" * 50) is None


def test_a_pdf_with_binary_streams_is_scanned_quickly() -> None:
    import random

    noise = random.Random(1).randbytes(1_500_000)  # an embedded font or image
    text = zlib.compress(b"BT (Fiktivbolaget AB) Tj ET")
    pdf = b"%PDF-1.7\n" + b"stream\n" + zlib.compress(noise) + b"\nendstream\nstream\n" + text + b"\nendstream\n"
    start = time.monotonic()
    found = scan("fixtures/synthetic/big.pdf", pdf)
    assert time.monotonic() - start < 10
    assert "names" in kinds(found)
