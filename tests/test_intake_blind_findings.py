"""One test per confirmed review finding on blind intake (PR #8): the fail-closed prospective check, what a dev folder
may hold, the zip and page readers, and reading fields by label. Every tender, buyer and person here is invented."""

from __future__ import annotations

import io
import plistlib
import struct
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from test_intake_blind import NOW, TENDERS, expected, notice_page, zip_bytes

from anbud import cli, notice, paths
from anbud import intake as intake_module
from anbud.evals import split_heldout as sh
from anbud.intake import IntakeError, intake, survey

BUYER = "Påhittade Fastigheter"


def dev_name() -> str:
    return next(n for n, s in expected().items() if s == sh.DEV)


def dev_meta(deadline: str | None = None) -> tuple[str, ...]:
    meta = TENDERS[dev_name()]
    assert meta is not None
    return meta if deadline is None else (*meta[:4], deadline)


def incoming_with(data_root: Path, zips: dict[str, bytes]) -> Path:
    incoming = data_root / "incoming" / "co-002"
    incoming.mkdir(parents=True, exist_ok=True)
    for name, data in zips.items():
        (incoming / name).write_bytes(data)
    return incoming


def page(body: str, title: str = "Upphandling", head: str = "") -> bytes:
    head = f'<meta charset="utf-8"><title>{title}</title>{head}'
    return f"<!DOCTYPE html><html><head>{head}</head><body>{body}</body></html>".encode()


def labelled(deadline: str = "2026-12-15") -> str:
    return (
        f"<table><tr><th>Upphandlande myndighet</th><td>{BUYER}</td></tr>"
        "<tr><th>Diarienummer</th><td>UH-2026-0202</td></tr>"
        f"<tr><th>Sista anbudsdag</th><td>{deadline}</td></tr></table>"
    )


def read(*pages: tuple[str, bytes]) -> notice.Notice:
    return notice.from_pages([((name,), data) for name, data in pages])


def set_flag(data: bytes, member: str, local_byte: int, bit: int) -> bytes:
    """Set a flag bit on one member, in its local header (byte 6 or 7) and its central header (two bytes later)."""
    raw = bytearray(data)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        info = zf.getinfo(member)
    raw[info.header_offset + local_byte] |= bit
    central = raw.find(b"PK\x01\x02")
    while central != -1:
        name_len = struct.unpack("<H", raw[central + 28 : central + 30])[0]
        if bytes(raw[central + 46 : central + 46 + name_len]) == info.filename.encode():
            raw[central + local_byte + 2] |= bit
            break
        central = raw.find(b"PK\x01\x02", central + 4)
    return bytes(raw)


def encrypted(data: bytes, member: str) -> bytes:
    return set_flag(data, member, 6, 0x01)


def damaged_deflate(entries: dict[str, bytes | str], member: str) -> bytes:
    """A zip whose `member` has a deflate stream that starts with an invalid block type: reading it raises zlib.error,
    which is not an OSError."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    raw = bytearray(buf.getvalue())
    with zipfile.ZipFile(io.BytesIO(bytes(raw))) as zf:
        offset = zf.getinfo(member).header_offset
    name_len, extra_len = struct.unpack("<HH", raw[offset + 26 : offset + 30])
    raw[offset + 30 + name_len + extra_len] = 0x07  # final block, type 3: invalid
    return bytes(raw)


def dev_folders(data_root: Path) -> list[Path]:
    dev = data_root / "co-002" / "dev"
    return sorted(dev.iterdir()) if dev.exists() else []


# High: the prospective check fails closed.


def test_a_zip_with_no_deadline_read_is_prospective_never_dev(data_root: Path) -> None:
    no_label = notice_page(*dev_meta()).replace(b"Sista anbudsdag", b"Kommentar")
    incoming = incoming_with(
        data_root,
        {
            dev_name(): zip_bytes({"Annons.html": no_label, "AF.txt": "a"}),
            "Utan annons.zip": zip_bytes({"AF.txt": "b"}),
        },
    )
    result = intake("co-002", incoming, new_ledger=True, now=NOW)
    assert (result.prospective, result.dev, result.heldout) == (2, 0, 0)
    assert result.notices.no_deadline == 2
    assert dev_folders(data_root) == []


@pytest.mark.parametrize(
    "label",
    [
        "Anbudsfrist",
        "Sista dag för anbudsinlämning",
        "Sista tid för anbud",
        "Anbud ska ha kommit in senast",
        "Deadline for receipt of tenders",
        "Time limit for receipt of tenders",
    ],
)
def test_bid_deadline_labels_are_read(label: str) -> None:
    assert read(("a.html", page(f"<p>{label}: 2026-12-20 12:00</p>"))).deadline == datetime(2026, 12, 20, 12, 0)


def test_the_latest_deadline_on_any_page_counts_an_amendment_page_too(data_root: Path) -> None:
    annons = notice_page(*dev_meta("2026-11-20"))  # passed by NOW
    change = page("<p>Sista anbudsdag: 2026-12-20</p>", title="Ändringsmeddelande")
    assert read(("Annons.html", annons), ("sida2.html", change)).deadline == datetime(2026, 12, 20)
    incoming = incoming_with(data_root, {dev_name(): zip_bytes({"Annons.html": annons, "sida2.html": change})})
    result = intake("co-002", incoming, new_ledger=True, now=NOW)
    assert result.prospective == 1 and result.notices.open == 1 and dev_folders(data_root) == []


@pytest.mark.parametrize(
    ("extra", "sealed"),
    [
        ({"Ändring 1.txt": "x"}, True),  # an amendment may have moved the deadline
        ({"Frågor och svar.txt": "x"}, True),  # so may an answer
        ({"Ändring 1.txt": "x", "Tilldelningsbeslut.txt": "x"}, False),  # awarded: closed for sure
        ({}, False),  # nothing that could have moved it
    ],
)
def test_a_deadline_passed_lately_is_sealed_when_something_may_have_moved_it(
    data_root: Path, extra: dict[str, str], sealed: bool
) -> None:
    recent = (NOW - timedelta(days=20)).date().isoformat()
    entries: dict[str, bytes | str] = {"Annons.html": notice_page(*dev_meta(recent)), "AF.txt": "a", **extra}
    incoming = incoming_with(data_root, {dev_name(): zip_bytes(entries)})
    result = intake("co-002", incoming, new_ledger=True, now=NOW)
    assert (result.prospective, result.dev) == ((1, 0) if sealed else (0, 1))
    assert result.notices.may_have_moved == sealed


# High: a notice page is never thrown away for its title.


@pytest.mark.parametrize(
    ("title", "body_head"),
    [
        ("Kompletterande markarbeten etapp 2", ""),
        ("Tillägg förskola", ""),
        ("Ändring av ventilation skola", ""),
        ("Upphandling", "<nav><svg><title>Frågor och svar</title></svg></nav>"),  # an icon's title
    ],
)
def test_a_notice_named_like_an_amendment_or_with_an_icon_title_is_read(title: str, body_head: str) -> None:
    n = read(("annons.html", page(body_head + labelled(), title=title)))
    assert n.pages == 1 and n.buyer == BUYER and n.deadline == datetime(2026, 12, 15)
    assert not n.answer_pages


def test_a_labelled_page_titled_as_questions_and_answers_is_read_and_still_held() -> None:
    n = read(("sida.html", page(labelled("2026-12-18"), title="Frågor och svar")))
    assert n.buyer == BUYER and n.deadline == datetime(2026, 12, 18)
    assert n.answer_pages == {"sida.html"}


# Medium: what a dev folder may hold. (The original zip and inline Q&A sections are also checked over the whole
# fixture in test_intake_blind.py.)


def test_the_original_zip_goes_to_answers_and_the_notice_copy_leaves_out_its_sections(data_root: Path) -> None:
    secret = "Fråga 7 om fiktiv kabel"
    annons = notice_page(*dev_meta()).replace(
        b"<h2>Dokument</h2>", f"<h2>Frågor och svar</h2><p>{secret}</p><h2>Dokument</h2>".encode()
    )
    incoming = incoming_with(data_root, {dev_name(): zip_bytes({"Annons.html": annons, "AF.txt": "a"})})
    assert intake("co-002", incoming, new_ledger=True, now=NOW).dev == 1
    (tender,) = dev_folders(data_root)
    files = [p for p in tender.rglob("*") if p.is_file()]
    assert not any(p.suffix == ".zip" for p in files)
    assert not any(secret.encode() in p.read_bytes() for p in files)
    copy = (tender / "inbox" / "Annons.html").read_text(encoding="utf-8")
    assert dev_meta()[0] in copy and "Administrativa föreskrifter" in copy
    answers = paths.answers_dir("co-002", tender.name)
    assert len(list((answers / "originals").glob("*.zip"))) == 1
    assert any(secret.encode() in p.read_bytes() for p in (answers / "intake").iterdir())


def test_archives_that_cant_be_unpacked_are_held(data_root: Path) -> None:
    locked = encrypted(zip_bytes({"A-101.txt": "ritning"}), "A-101.txt")
    entries: dict[str, bytes | str] = {
        "Annons.html": notice_page(*dev_meta()),
        "AF.txt": "a",
        "Ritningar.zip": locked,
        "Bilagor.7z": b"7z\xbc\xaf\x27\x1c",
    }
    incoming = incoming_with(data_root, {dev_name(): zip_bytes(entries)})
    result = intake("co-002", incoming, new_ledger=True, now=NOW)
    assert result.dev == 1 and result.held.get("archive") == 2
    (tender,) = dev_folders(data_root)
    assert sorted(p.name for p in (tender / "inbox").iterdir()) == ["AF.txt", "Annons.html"]


def test_an_outer_zip_that_cant_be_unpacked_is_tried_once_and_kept_out_of_the_inbox(data_root: Path) -> None:
    data = encrypted(zip_bytes({"Annons.html": notice_page(*dev_meta()), "AF.txt": "a"}), "AF.txt")
    incoming = incoming_with(data_root, {dev_name(): data})
    result = intake("co-002", incoming, new_ledger=True, now=NOW)
    assert result.dev == 1 and result.failed == []
    assert (result.unpack.archives_kept, result.unpack.archives_unpacked) == (1, 0)
    (tender,) = dev_folders(data_root)
    assert list((tender / "inbox").iterdir()) == []
    assert (paths.answers_dir("co-002", tender.name) / "originals").is_dir()


def test_a_failure_while_committing_the_answers_leaves_nothing_behind(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = zip_bytes({"Annons.html": notice_page(*dev_meta()), "Frågor och svar.txt": "x", "AF.txt": "a"})
    incoming = incoming_with(data_root, {dev_name(): data})
    real_rename = intake_module.os.rename

    def fail_answers(src: object, dst: object) -> None:
        if Path(dst) == paths.answers_dir("co-002", "t-0001"):
            raise OSError("no space")
        real_rename(src, dst)

    with monkeypatch.context() as patched:
        patched.setattr(intake_module.os, "rename", fail_answers)
        result = intake("co-002", incoming, new_ledger=True, now=NOW)
    assert result.failed == ["t-0001: OSError"]
    assert (incoming / dev_name()).read_bytes() == data
    answers = data_root / "answers" / "co-002"
    assert not answers.exists() or list(answers.iterdir()) == []
    assert dev_folders(data_root) == []
    assert intake("co-002", incoming, now=NOW).dev == 1


def test_a_crash_after_the_answers_moved_is_undone_by_the_next_run(data_root: Path) -> None:
    data = zip_bytes({"Annons.html": notice_page(*dev_meta()), "Frågor och svar.txt": "x", "AF.txt": "a"})
    incoming = incoming_with(data_root, {dev_name(): data})
    intake("co-002", incoming, new_ledger=True, now=NOW)
    # Rebuild the state a crash between the answers' rename and the tender's leaves: the tender's folder back in
    # staging, the delivery in answers/originals.
    tender = paths.dev_tender_dir("co-002", "t-0001")
    tender.rename(tender.with_name(".staging-t-0001"))
    result = intake("co-002", incoming, now=NOW)  # recovers, then places the delivery again
    assert result.dev == 1 and (incoming / dev_name()).exists() is False
    assert paths.dev_tender_dir("co-002", "t-0001").is_dir()


# Medium and low: readers that must not stop an intake.


def test_a_damaged_deflate_stream_stops_neither_the_reader_nor_the_intake(data_root: Path) -> None:
    meta = dev_meta()
    bad_page = damaged_deflate({"Annons.html": notice_page(*meta), "AF.txt": "a" * 200}, "Annons.html")
    bad_doc = damaged_deflate({"Annons.html": notice_page(*meta), "AF.txt": "a" * 200}, "AF.txt")
    path = data_root / "bad.zip"
    path.write_bytes(bad_page)
    assert notice.from_zip(path).pages == 0  # the page can't be read: no notice, and no exception
    incoming = incoming_with(data_root, {"Utan annons.zip": bad_page, dev_name(): bad_doc})
    result = intake("co-002", incoming, new_ledger=True, now=NOW)
    assert result.failed == [] and result.dev == 1 and result.prospective == 1
    assert result.unpack.archives_kept == 1  # the damaged delivery is kept whole, with the answers


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("a.html", page("<p>Sista anbudsdag: 2026-12-20</p>", head="<meta charset='idna'>")),
        ("a.webarchive", b"<?xml version='1.0'?><plist><dict><key>WebMainResource</key>"),
        ("a.webarchive", plistlib.dumps({"WebMainResource": ["inte en ordbok"]}, fmt=plistlib.FMT_BINARY)),
        ("a.mhtml", b"MIME-Version: 1.0\r\nContent-Type: multipart/related; boundary=\r\n\r\n--\r\n"),
    ],
)
def test_an_odd_page_never_raises(name: str, data: bytes) -> None:
    n = notice.from_pages([((name,), data)])
    assert isinstance(n, notice.Notice)


def test_a_dry_run_refuses_held_out_storage(data_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sealed = data_root / "heldout" / "co-002" / "t-0001" / "originals"
    sealed.mkdir(parents=True)
    (sealed / "abc.zip").write_bytes(zip_bytes({"Annons.html": notice_page(*dev_meta())}))
    with pytest.raises(IntakeError, match="held-out or sealed"):
        survey(sealed)
    assert cli.main(["intake", "co-002", str(sealed), "--dry-run"]) == cli.REFUSED
    assert "held-out or sealed" in capsys.readouterr().err


def test_dry_run_and_real_run_hold_the_same_pages_in_nested_zips(data_root: Path) -> None:
    qa = page("<p>Svar om kabel.</p>", title="Frågor och svar")
    nested = zip_bytes({"sida.html": qa, "beskrivning.txt": "text"})
    data = zip_bytes({"Annons.html": notice_page(*dev_meta()), "AF.txt": "a", "Bilagor.zip": nested})
    incoming = incoming_with(data_root, {dev_name(): data})
    planned, _ = survey(incoming, NOW)
    assert planned.held == {"page": 1}
    placed = intake("co-002", incoming, new_ledger=True, now=NOW)
    assert sum(placed.held.values()) == 1  # held as "qa", by the page's title


def test_member_paths_match_what_intake_unpacks_for_names_without_the_utf8_flag() -> None:
    data = zip_bytes({"Bilagor/frågor.html": "x"})
    flagless = bytearray(data)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        info = zf.infolist()[0]
    assert info.flag_bits & 0x800
    flagless[info.header_offset + 7] &= ~0x08 & 0xFF
    central = flagless.find(b"PK\x01\x02")
    flagless[central + 9] &= ~0x08 & 0xFF
    with zipfile.ZipFile(io.BytesIO(bytes(flagless))) as zf:
        (raw,) = zf.infolist()
    assert raw.filename != "Bilagor/frågor.html"  # read as cp437 by zipfile
    assert notice.member_parts(raw) == ("Bilagor", "frågor.html")
    assert notice.member_parts(raw, notice.nested_prefix(("docs", "inre.zip"))) == (
        "docs",
        "inre",
        "Bilagor",
        "frågor.html",
    )


# Medium and low: reading fields by label.


def test_a_deadline_range_gives_its_end_and_other_deadlines_are_not_the_bid_deadline() -> None:
    assert read(("a.html", page("<p>Anbudstid: 2026-11-01 – 2026-12-15</p>"))).deadline == datetime(2026, 12, 15)
    eforms = page(
        "<p>Deadline for requesting additional information: 05/12/2026 23:59</p>"
        "<p>Deadline for receipt of tenders: 15/12/2026 12:00</p>"
    )
    assert read(("a.html", eforms)).deadline == datetime(2026, 12, 15, 12, 0)
    assert read(("a.html", page("<p>Deadline for questions: 2026-12-05</p>"))).deadline is None


@pytest.mark.parametrize("text", ["15 december, 2026", "December 15, 2026", "Dec 15th, 2026 at 12:00"])
def test_more_date_forms(text: str) -> None:
    parsed = notice.parse_datetime(text)
    assert parsed is not None and parsed.date() == datetime(2026, 12, 15).date()


def test_the_preferred_label_wins_over_the_first_line() -> None:
    body = (
        "<p>Organisation: Exempelplattformen AB</p><p>Upphandlare: Anna Exempel</p>"
        f"<p>Officiellt namn: {BUYER}</p><p>Diarienummer: KS 2026/123</p>"
    )
    assert read(("a.html", page(body))).buyer == BUYER


def test_the_platforms_org_number_is_never_the_buyers() -> None:
    footer = "<footer><p>Org nr: 556000-0001</p></footer>"
    body = f"<main><p>Upphandlande organisation</p><p>{BUYER}</p><p>Diarienummer: UH-1</p></main>{footer}"
    assert read(("a.html", page(body))).org_number is None
    block = (
        f"<header><p>Org nr: 556000-0001</p></header><p>Official name: {BUYER}</p>"
        "<p>Registration number: 212000-0002</p><p>Deadline for receipt of tenders: 2026-12-15</p>"
    )
    assert read(("a.html", page(block))).org_number == "2120000002"


def test_an_org_number_every_zip_in_a_batch_shares_is_dropped(data_root: Path) -> None:
    zips = {}
    for i, buyer in enumerate(("Fiktiva kommunen", "Påhittade regionen", "Exempelverket")):
        body = f"<p>Upphandlande myndighet: {buyer}</p><p>Org nr: 556000-0001</p><p>Sista anbudsdag: 2026-06-1{i}</p>"
        zips[f"u{i}.zip"] = zip_bytes({"Annons.html": page(body)})
    incoming = incoming_with(data_root, zips)
    deliveries = intake_module._zip_deliveries(incoming, NOW)
    assert all(d.notice is not None and d.notice.org_number is None for d in deliveries)
    assert len({d.keys for d in deliveries}) == 3  # each grouped by its own buyer


def test_a_label_and_value_in_sibling_inline_elements_are_read() -> None:
    body = (
        "<div><span>Sista anbudsdag</span><span>2026-12-15</span></div>"
        "<div><b>Diarienummer</b><span>KS 2026/123</span></div>"
        f"<div><span>Upphandlande myndighet</span><span>{BUYER}</span></div>"
    )
    n = read(("a.html", page(body)))
    assert (n.deadline, n.reference, n.buyer) == (datetime(2026, 12, 15), "KS 2026/123", BUYER)


def test_a_contact_line_is_not_the_reference() -> None:
    body = "<p>Referens: Anna Exempel, 070-123 45 67</p><p>Diarienummer: KS 2026/123</p>"
    assert read(("a.html", page(body))).reference == "KS 2026/123"
    assert read(("a.html", page("<p>Referensnummer: Anna Exempel, 070-123 45 67</p>"))).reference is None
