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


def test_oversized_files_and_zip_bombs_are_skipped_not_read(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources, "MAX_UNZIPPED_BYTES", 1000)
    monkeypatch.setattr(sources, "MAX_FILE_BYTES", 6000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/workbook.xml", "0" * 5000)
    big = b"%PDF-1.7 " + b"0" * 7000
    folder = inbox(data_root, {"bomb.xlsx": buf.getvalue(), "big.pdf": big})
    loaded = load_folder(folder)
    assert sorted((s.too_large, s.kind, s.data) for s in loaded.sources) == [(True, "other", b"")] * 2
    assert hashlib.sha256(big).hexdigest() in {s.sha256 for s in loaded.sources}  # hashed in a stream
    assert {s.size for s in loaded.sources} == {len(buf.getvalue()), len(big)}
    summary = tender.prepare("co-001", folder, folder.parent / "prep", root=data_root)
    assert summary["skipped"] == 2


def test_a_linked_inbox_manifest_or_escaping_folder_is_refused(data_root: Path, tmp_path: Path) -> None:
    pdf = text_pdf("En rad text som är tillräckligt lång för ett textlager.")
    folder = inbox(data_root, {"a.pdf": pdf})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.pdf").write_bytes(pdf + b"%other")
    (folder / "sub").symlink_to(outside, target_is_directory=True)
    assert load_folder(folder).skipped_links == 1 and len(load_folder(folder).sources) == 1
    linked = folder.parent / "linked-inbox"
    linked.symlink_to(folder, target_is_directory=True)
    with pytest.raises(SourceError, match="link"):
        load_folder(linked)
    real = folder.parent / "real.sha256"
    real.write_text(f"{hashlib.sha256(pdf).hexdigest()}  a.pdf\n")
    (folder.parent / "inbox.sha256").symlink_to(real)
    with pytest.raises(SourceError, match="regular file"):
        load_folder(folder, folder.parent / "inbox.sha256")


def test_a_sealed_inbox_is_refused(data_root: Path) -> None:
    sealed = data_root / "heldout" / "co-001" / "t-0002" / "docs"
    sealed.mkdir(parents=True)
    with pytest.raises(tender.PrepError, match="sealed"):
        tender.prepare("co-001", sealed, data_root / "co-001" / "x" / "prep", root=data_root)


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


def test_a_marked_file_leaves_no_cache_no_tiles_and_removes_an_earlier_prep(data_root: Path) -> None:
    clean = text_pdf("En rad text som är tillräckligt lång för ett textlager.")
    folder = inbox(data_root, {"a.pdf": clean})
    out = folder.parent / "prep"
    tender.prepare("co-001", folder, out, root=data_root)
    assert (out / "blocks.jsonl").exists()
    company = paths.company_dir("co-001", data_root)
    blobs_before = sorted((company / "blobs").rglob("*")) if (company / "blobs").exists() else []
    marked = make_pdf(
        [
            Page(texts=[Text(60, 100, "Ritningen omfattas av säkerhetsskydd enligt lagen.", 10)]),
            Page(size=A2, image=(120, 170)),
        ]
    )
    (folder / "b.pdf").write_bytes(marked)
    with pytest.raises(tender.IntakeStop):
        tender.prepare("co-001", folder, out, root=data_root)
    assert not out.exists()  # the earlier outputs no longer describe the inbox
    sha = hashlib.sha256(marked).hexdigest()
    assert not list((company / "prep-cache").rglob(f"{sha}.json"))
    assert not (company / "prep-staging" / sha).exists()
    after = sorted((company / "blobs").rglob("*")) if (company / "blobs").exists() else []
    assert after == blobs_before  # the marked drawing's tiles never reached the blob store


def test_outputs_are_swapped_in_whole(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = inbox(data_root, {"a.txt": "Första stycket.".encode()})
    out = folder.parent / "prep"
    tender.prepare("co-001", folder, out, root=data_root)
    first = (out / "blocks.jsonl").read_bytes()
    (folder / "b.txt").write_bytes(b"Andra filen.")

    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(tender, "_write_outputs", broken)
    with pytest.raises(OSError):
        tender.prepare("co-001", folder, out, root=data_root)
    assert not out.exists() and not list(folder.parent.glob(".prep.*"))
    monkeypatch.undo()
    tender.prepare("co-001", folder, out, root=data_root)
    assert (out / "blocks.jsonl").read_bytes() != first and not list(folder.parent.glob(".prep.*"))


def test_a_multi_page_tiff_is_one_page_per_frame(data_root: Path) -> None:
    from PIL import Image

    frames = [Image.new("RGB", (400, 300), c) for c in ("white", "gray", "black")]
    buf = io.BytesIO()
    frames[0].save(buf, format="TIFF", save_all=True, append_images=frames[1:])
    folder = inbox(data_root, {"skanning.tif": buf.getvalue()})
    summary = tender.prepare("co-001", folder, folder.parent / "prep", root=data_root)
    assert summary["pages_by_kind"] == {"scan": 3} and summary["tiles"] == 3


def test_an_image_over_the_limit_is_skipped_with_its_own_label_and_count(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PIL import Image

    monkeypatch.setattr(tender, "MAX_IMAGE_PIXELS", 100_000)
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), "white").save(buf, format="PNG")  # 120 000 pixels
    damaged = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    folder = inbox(data_root, {"stor.png": buf.getvalue(), "trasig.png": damaged})
    out = folder.parent / "prep"
    summary = tender.prepare("co-001", folder, out, root=data_root)
    assert summary["skipped"] == 2 and summary["skipped_over_image_limit"] == 1
    reasons = sorted(d["skipped"] for d in json.loads((out / "documents.json").read_text()))
    assert reasons == sorted([tender.OVER_IMAGE_LIMIT, "unreadable, damaged or password-protected"])


def test_prep_writes_which_documents_the_models_will_read(data_root: Path) -> None:
    files = {
        "El/E-63-1-001 Teknisk beskrivning el.pdf": text_pdf("Uttag 3 st i förråd."),
        "VS/R-50-1-001 Teknisk beskrivning VS.pdf": text_pdf("Rör 30 m.", "Pumpar elansluts av elentreprenören."),
    }
    folder = inbox(data_root, files)
    out = folder.parent / "prep"
    summary = tender.prepare("co-001", folder, out, root=data_root)
    assert summary["selection"]["documents_excluded"] == 1 and summary["selection"]["boundary_lines"] == 1
    saved = json.loads((out / "selection.json").read_text())
    assert sorted((d["discipline"], d["read"]) for d in saved["documents"]) == [("el", True), ("other", False)]
    assert json.loads((out / "summary.json").read_text())["selection"] == summary["selection"]
    assert "Teknisk" not in (out / "selection.json").read_text()  # ids, classes and signals, never a name


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
