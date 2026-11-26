from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import zipfile
from pathlib import Path

import pytest
from conftest import assert_no_fake_names
from synthetic_docs import A2, Page, Text, make_pdf
from test_runguard import ledger_with

from anbud import cli, paths
from anbud.evals import split_heldout as sh
from anbud.prep import sources, tender
from anbud.prep.sources import SourceError, detect_kind, load_folder


def text_pdf(*lines: str) -> bytes:
    return make_pdf([Page(texts=[Text(60, 100 + 20 * i, line, 10) for i, line in enumerate(lines)])])


def inbox(data_root: Path, files: dict[str, bytes], tender_id: str = "t-0001") -> Path:
    folder = paths.dev_tender_dir("co-001", tender_id, data_root) / "inbox"
    folder.mkdir(parents=True)
    for name, data in files.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)
    return folder


def test_detect_kind_by_content() -> None:
    assert detect_kind(b"%PDF-1.7 ...", "x.bin") == "pdf"
    assert detect_kind(b"AC1032....", "x") == "dwg"
    assert detect_kind(b"ISO-10303-21;\nHEADER;", "x") == "ifc"
    assert detect_kind(b"\x89PNG\r\n\x1a\n....", "x") == "image"
    assert detect_kind(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "x.xls") == "legacy_office"
    assert detect_kind(b"hej", "notes.txt") == "text"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/workbook.xml", "<workbook/>")
    assert detect_kind(buf.getvalue(), "renamed.pdf") == "xlsx"


def test_identical_files_are_one_document_and_links_and_hidden_files_are_skipped(data_root: Path) -> None:
    pdf = text_pdf("En rad text som är tillräckligt lång för ett textlager.")
    folder = inbox(data_root, {"a.pdf": pdf, "sub/b.pdf": pdf, ".DS_Store": b"x"})
    (folder / "link.pdf").symlink_to(folder / "a.pdf")
    loaded = load_folder(folder)
    assert len(loaded.sources) == 1 and loaded.sources[0].paths == ("a.pdf", "sub/b.pdf")
    assert loaded.skipped_links == 1 and loaded.skipped_hidden == 1


def test_the_intake_manifest_must_match(data_root: Path) -> None:
    pdf = text_pdf("En rad text som är tillräckligt lång för ett textlager.")
    folder = inbox(data_root, {"a.pdf": pdf})
    manifest = folder.parent / "inbox.sha256"
    manifest.write_text(f"{hashlib.sha256(pdf).hexdigest()}  a.pdf\n")
    assert len(load_folder(folder, manifest).sources) == 1
    (folder / "b.pdf").write_bytes(pdf + b"%changed")
    with pytest.raises(SourceError, match="1 unexpected, 0 missing"):
        load_folder(folder, manifest)


def test_a_zip_that_unpacks_too_large_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources, "MAX_UNZIPPED_BYTES", 1000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/workbook.xml", "0" * 5000)
    with pytest.raises(SourceError, match="unpacks"):
        detect_kind(buf.getvalue(), "big.xlsx")


def test_prepare_writes_private_outputs_and_caches(data_root: Path) -> None:
    folder = inbox(
        data_root,
        {
            "beskrivning.pdf": text_pdf("1.1 Omfattning av arbetena", "Kontakt via c.exempel@example.se gärna."),
            "ritning.pdf": make_pdf([Page(size=A2, image=(120, 170))]),
            "noter.txt": "Första stycket.\nSamma stycke.\n\nAndra stycket.".encode(),
            "modell.ifc": b"ISO-10303-21;\nHEADER;",
            "trasig.pdf": b"%PDF-1.4 not really a pdf",
        },
    )
    out = folder.parent / "prep"
    summary = tender.prepare("co-001", folder, out, root=data_root)
    assert summary["documents"] == 5 and summary["skipped"] == 2
    assert summary["tiles"] == 2 and summary["masked_by_kind"] == {"email": 1}
    assert summary["cache_hits"] == 0
    for name in ("documents.json", "blocks.jsonl", "outline.json", "tiles.json", "masking.jsonl", "summary.json"):
        assert stat.S_IMODE(os.stat(out / name).st_mode) == 0o600
    docs = json.loads((out / "documents.json").read_text())
    reasons = sorted(d["skipped"] for d in docs if d["skipped"])
    assert reasons == ["ifc files aren't read yet", "unreadable, damaged or password-protected"]
    blocks = [json.loads(line) for line in (out / "blocks.jsonl").read_text().splitlines()]
    assert {"Första stycket. Samma stycke.", "Andra stycket."} <= {b["text"] for b in blocks}
    before = (out / "blocks.jsonl").read_bytes()
    again = tender.prepare("co-001", folder, out, root=data_root)
    assert again["cache_hits"] == 5
    assert (out / "blocks.jsonl").read_bytes() == before


def test_a_security_marking_stops_before_any_output(data_root: Path) -> None:
    folder = inbox(data_root, {"a.pdf": text_pdf("Handlingen är HEMLIG enligt beslut från myndigheten.")})
    out = folder.parent / "prep"
    with pytest.raises(tender.IntakeStop) as stop:
        tender.prepare("co-001", folder, out, root=data_root)
    assert "hemlig" in str(stop.value) and "HEMLIG" not in str(stop.value)
    assert not out.exists()


def test_cli_prep_only_for_dev_tenders_and_prints_counts(data_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    secret_line = "Fiktivbolaget AB levererar enligt avtal till Påhittade Fastigheter i år."
    inbox(data_root, {"a.pdf": text_pdf(secret_line)})
    assert cli.main(["prep", "co-001", "t-0001"]) == cli.REFUSED  # not in the ledger yet
    ledger_with(data_root, t_0001=sh.DEV, t_0002=sh.HELDOUT)
    assert cli.main(["prep", "co-001", "t-0002"]) == cli.REFUSED
    capsys.readouterr()
    assert cli.main(["prep", "co-001", "t-0001"]) == 0
    printed = capsys.readouterr()
    assert "blocks_by_kind" in printed.out
    assert_no_fake_names(printed.out, printed.err)
