"""Blind intake of public tenders: one zip each, metadata read from the notice page by code, answer keys held back."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from conftest import assert_no_fake_names

from anbud import cli, notice, paths
from anbud import intake as intake_module
from anbud.evals import split_heldout as sh
from anbud.intake import IntakeError, intake, notice_keys
from anbud.prep.tender import prepare

NOW = datetime(2026, 11, 27, 9, 0)


def notice_page(buyer: str, org: str, ref: str, published: str, deadline: str, listed: str = "") -> bytes:
    """A platform notice page, invented. Labels in a table, the way several platforms lay them out."""
    qa = f'<h2>Frågor och svar</h2><p>Fråga 1 om Fiktivbolaget</p><a href="dl/{listed}">{listed}</a>' if listed else ""
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Upphandling</title>
<script>var x = "Fiktivbolaget";</script></head><body><nav>Meny</nav><h1>Ombyggnad</h1>
<table><tr><th>Upphandlande myndighet</th><td>{buyer}</td></tr>
<tr><th>Organisationsnummer</th><td>{org}</td></tr><tr><th>Diarienummer</th><td>{ref}</td></tr>
<tr><th>Publicerad</th><td>{published}</td></tr><tr><th>Sista anbudsdag</th><td>{deadline}</td></tr>
<tr><th>Förfarande</th><td>Öppet förfarande</td></tr></table><p>CPV: 45310000-3 Elinstallation</p>{qa}
<h2>Dokument</h2><a href="dl/af.pdf">Administrativa föreskrifter</a></body></html>""".encode()


def zip_bytes(entries: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


# Six invented tenders. Buyers differ, so the seeded hash spreads them; "Påhittade" buys twice, so those two are one
# group. One deadline is still open (prospective); one zip has no notice page at all.
TENDERS = {
    "Fiktivbolaget kontor.zip": ("Fiktivbolaget AB", "202100-0001", "Dnr 2026/0101", "2026-05-02", "2026-06-10"),
    "Påhittade skola.zip": ("Påhittade Fastigheter", "212000-0002", "UH-2026-0202", "2026-09-03", "2026-10-20"),
    "Påhittade bad.zip": ("Påhittade Fastigheter", "212000-0002", "UH-2026-0303", "2026-10-01", "2026-11-02"),
    "Örjan sjukhus.zip": ("Örjan Exempelsson region", "232100-0004", "RS 2026-0404", "3 oktober 2026", "1 nov 2026"),
    "Kommande depå.zip": ("Ampere kommun", "212000-0005", "KS-2026-0505", "2026-11-10", "2027-01-15 12:00"),
    "Utan annons.zip": None,
}


def tender_zip(name: str, meta: tuple[str, ...] | None) -> bytes:
    docs: dict[str, bytes | str] = {
        "Förfrågningsunderlag/AF.txt": "Administrativa föreskrifter.",
        "Förfrågningsunderlag/Teknisk beskrivning.txt": "Teknisk beskrivning el.",
        "Ritningar.zip": zip_bytes({"E-10.txt": "ritning", "Inre.zip": zip_bytes({"E-11.txt": "ritning 2"})}),
        "Frågor och svar/FoS 1.txt": "Fråga: ... Svar: ...",
        "Kompletteringar/Ändring 1.txt": "Ändring av mängd.",
        "Dokument 7.txt": "Svar på fråga 2.",  # neutral name; the notice lists it under its Q&A heading
        "fragor.html": "<html><head><title>Frågor och svar</title></head><body>Svar</body></html>",
        "__MACOSX/._AF.txt": "fork",
    }
    if meta is not None:
        docs["Annons.html"] = notice_page(*meta, listed="Dokument 7.txt")
    return zip_bytes(docs)


def build(root: Path) -> Path:
    incoming = root / "incoming" / "co-002"
    incoming.mkdir(parents=True)
    for name, meta in TENDERS.items():
        (incoming / name).write_bytes(tender_zip(name, meta))
    return incoming


def expected() -> dict[str, str]:
    rows = []
    for i, (name, meta) in enumerate(sorted(TENDERS.items()), 1):
        n = notice.from_pages([(("Annons.html",), notice_page(*meta))]) if meta else notice.Notice()
        keys, _ = notice_keys(n)
        rows.append(sh.TenderRow(f"t-{i:04d}", keys, prospective=name.startswith("Kommande")))
    state = sh.LedgerState()
    for event in sh.plan_split(state, rows):
        if event["event"] == "assign":
            state.status[event["tender"]] = event["status"]
    return {name: state.status[f"t-{i:04d}"] for i, name in enumerate(sorted(TENDERS), 1)}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_the_fixture_spreads_over_dev_held_out_and_prospective() -> None:
    status = expected()
    assert {sh.DEV, sh.HELDOUT, sh.PROSPECTIVE} <= set(status.values())
    assert status["Påhittade skola.zip"] == status["Påhittade bad.zip"]  # one buyer, one group


def test_blind_intake_splits_by_the_notice_and_prints_counts_only(
    data_root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    incoming = build(data_root)
    delivered = {name: sha((incoming / name).read_bytes()) for name in TENDERS}
    monkeypatch.setattr(intake_module, "datetime", type("D", (), {"now": staticmethod(lambda: NOW)}))
    assert cli.main(["intake", "co-002", str(incoming), "--new-ledger", "--company-name", "Offentliga"]) == 0
    printed = capsys.readouterr()
    assert_no_fake_names(printed.out, printed.err)
    for word in ("Annons", "Frågor", "Dokument", "Ritningar", "2026/0101", "202100"):
        assert word not in printed.out + printed.err
    status = expected()
    counts = {s: sum(1 for v in status.values() if v == s) for s in (sh.DEV, sh.HELDOUT, sh.PROSPECTIVE)}
    assert f"{counts[sh.DEV]} dev, {counts[sh.HELDOUT]} held-out, {counts[sh.PROSPECTIVE]} prospective" in printed.out
    assert "notices: 6 read, 1 without a notice page" in printed.out
    assert "1 without a buyer key, 1 without any key" in printed.out
    assert "1 published before, 4 after, 1 unknown" in printed.out

    ledger = sh.load_ledger(paths.ledger_path("co-002")).status
    for i, name in enumerate(sorted(TENDERS), 1):
        tid = f"t-{i:04d}"
        assert ledger[tid] == status[name]
        if status[name] == sh.DEV:
            tender = paths.dev_tender_dir("co-002", tid)
            inbox = tender / "inbox"
            files = {p.relative_to(inbox).as_posix() for p in inbox.rglob("*") if p.is_file()}
            assert "Förfrågningsunderlag/AF.txt" in files
            assert "Ritningar/Inre/E-11.txt" in files  # nested zips unpacked
            assert not any("Frågor" in f or "Kompletteringar" in f or f == "fragor.html" for f in files)
            assert ("Dokument 7.txt" in files) == (TENDERS[name] is None)  # held only where a notice lists it
            assert not any("__MACOSX" in f for f in files)
            assert (tender / "originals" / f"{delivered[name]}.zip").exists()
            record = json.loads((tender / "intake.json").read_text())
            assert record["tender"] == tid and "listed" not in record["notice"]
            held = paths.answers_dir("co-002", tid) / "intake"
            lines = (held.parent / "intake.sha256").read_text().splitlines()
            kinds = sorted(line.split("  ")[1] for line in lines)
            assert kinds == (
                ["amendment", "qa", "qa"] if TENDERS[name] is None else ["amendment", "listed", "qa", "qa"]
            )
            for line in lines:
                digest = line.split("  ")[0]
                assert any(p.name.startswith(digest) for p in held.iterdir())
            summary = prepare("co-002", inbox, tender / "prep", tender / "inbox.sha256")
            assert summary["documents"] >= 4
        else:
            target = data_root / "heldout" / "co-002" / tid
            (stored,) = (target / "originals").iterdir()
            assert sha(stored.read_bytes()) == delivered[name]  # byte-identical
            assert not (target / "inbox").exists()  # never unpacked
            assert not paths.answers_dir("co-002", tid).exists()
    assert not list(incoming.iterdir())


def test_held_out_ids_are_refused_by_prep_and_run(data_root: Path) -> None:
    from anbud.runguard import RunRefused, check_run_allowed

    incoming = build(data_root)
    intake("co-002", incoming, new_ledger=True, now=NOW)
    status = sh.load_ledger(paths.ledger_path("co-002")).status
    sealed = [t for t, s in status.items() if s != sh.DEV]
    assert sealed
    for tid in sealed:
        with pytest.raises(RunRefused):
            check_run_allowed("co-002", tid)
        assert cli.main(["prep", "co-002", tid]) == cli.REFUSED


def test_an_open_deadline_is_prospective_and_stays_so(data_root: Path) -> None:
    incoming = data_root / "incoming" / "co-002"
    incoming.mkdir(parents=True)
    meta = TENDERS["Kommande depå.zip"]
    assert meta is not None
    (incoming / "Kommande depå.zip").write_bytes(tender_zip("x", meta))
    result = intake("co-002", incoming, new_ledger=True, now=NOW)
    assert result.prospective == 1
    assert sh.load_ledger(paths.ledger_path("co-002")).is_prospective("t-0001")


def test_a_rerun_and_a_repeated_zip_change_nothing(data_root: Path) -> None:
    incoming = build(data_root)
    copies = {name: (incoming / name).read_bytes() for name in TENDERS}
    intake("co-002", incoming, new_ledger=True, now=NOW)
    ledger = paths.ledger_path("co-002").read_bytes()
    assert intake("co-002", incoming, now=NOW).counts() == {"dev": 0, "heldout": 0, "prospective": 0, "burned": 0}
    (incoming / "kopia.zip").write_bytes(copies["Påhittade skola.zip"])
    again = intake("co-002", incoming, now=NOW)
    assert again.already_in == 1 and paths.ledger_path("co-002").read_bytes() == ledger


def test_a_new_zip_under_a_registered_name_is_refused(data_root: Path) -> None:
    incoming = build(data_root)
    intake("co-002", incoming, new_ledger=True, now=NOW)
    (incoming / "Påhittade skola.zip").write_bytes(zip_bytes({"annat.txt": "en annan upphandling"}))
    with pytest.raises(IntakeError, match="name of a registered tender"):
        intake("co-002", incoming, now=NOW)


def test_a_folder_or_loose_file_in_a_blind_intake_is_refused(data_root: Path) -> None:
    incoming = build(data_root)
    (incoming / "lös fil.pdf").write_bytes(b"%PDF-1.4")
    with pytest.raises(IntakeError, match="1 item"):
        intake("co-002", incoming, new_ledger=True, now=NOW)
    assert not paths.ledger_path("co-002").exists()


def test_a_failed_dev_placement_rolls_back_the_held_answers_too(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    incoming = data_root / "incoming" / "co-002"
    incoming.mkdir(parents=True)
    name = next(n for n, s in expected().items() if s == sh.DEV)
    data = tender_zip(name, TENDERS[name])
    (incoming / name).write_bytes(data)
    real_rename = intake_module.os.rename

    def fail_last(src: object, dst: object) -> None:
        if Path(dst) == paths.dev_tender_dir("co-002", "t-0001"):
            raise OSError("no space")
        real_rename(src, dst)

    with monkeypatch.context() as patched:
        patched.setattr(intake_module.os, "rename", fail_last)
        result = intake("co-002", incoming, new_ledger=True, now=NOW)
    assert result.failed == ["t-0001: OSError"]
    assert (incoming / name).read_bytes() == data
    assert not paths.answers_dir("co-002", "t-0001").exists() or not any(
        paths.answers_dir("co-002", "t-0001").iterdir()
    )
    assert not list((data_root / "co-002" / "dev").iterdir())
    result = intake("co-002", incoming, now=NOW)
    assert result.dev == 1 and (paths.answers_dir("co-002", "t-0001") / "intake").is_dir()


def test_dry_run_counts_and_writes_nothing(data_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    incoming = build(data_root)
    before = sorted(p.relative_to(data_root) for p in data_root.rglob("*"))
    assert cli.main(["intake", "co-002", str(incoming), "--dry-run"]) == 0
    printed = capsys.readouterr()
    assert_no_fake_names(printed.out, printed.err)
    assert "dev" not in printed.out and "held-out" not in printed.out  # no status before the real split
    assert "6 zips" in printed.out and "found buyer 5" in printed.out
    # Three per zip by name (Q&A folder, amendments folder, fragor.html) and, where there is a notice, the one it lists.
    assert "held as answer keys: 23 files in 6 tenders (amendment 6, listed 5, qa 12)" in printed.out
    assert sorted(p.relative_to(data_root) for p in data_root.rglob("*")) == before


# The notice reader on its own.


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-12-15 kl. 23:59", datetime(2026, 12, 15, 23, 59)),
        ("2026-12-15T12:00", datetime(2026, 12, 15, 12, 0)),
        ("15/01/2027 12:00", datetime(2027, 1, 15, 12, 0)),
        ("15.01.2027", datetime(2027, 1, 15)),
        ("3 oktober 2026", datetime(2026, 10, 3)),
        ("1 nov. 2026 kl 14.00", datetime(2026, 11, 1, 14, 0)),
        ("15 December 2026", datetime(2026, 12, 15)),
        ("ingen tid", None),
        ("2026-13-40", None),
    ],
)
def test_dates(text: str, expected: datetime | None) -> None:
    assert notice.parse_datetime(text) == expected


def test_label_layouts_mhtml_and_webarchive() -> None:
    page = (
        "<html><body><dl><dt>Köpare:</dt><dd>Påhittade Fastigheter</dd><dt>Referensnummer: X-7700</dt>"
        "<dt>Sista dag för mottagande av anbud</dt><dd>15/01/2027 12:00</dd>"
        "<dt>Nationellt registreringsnummer</dt><dd>212000-9999</dd>"
        "<p>Huvudsaklig klassificering (cpv): 45000000 Byggarbete</p><p>Typ av förfarande: Förenklat förfarande</p>"
        "</dl></body></html>"
    ).encode()
    mhtml = (
        b'MIME-Version: 1.0\r\nContent-Type: multipart/related; boundary="b"\r\n\r\n--b\r\n'
        b"Content-Type: text/html; charset=utf-8\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\n"
        + page.replace(b"=", b"=3D")
        + b"\r\n--b--\r\n"
    )
    import plistlib

    webarchive = plistlib.dumps(
        {"WebMainResource": {"WebResourceData": page, "WebResourceTextEncodingName": "UTF-8"}}, fmt=plistlib.FMT_BINARY
    )
    for name, data in (("a.html", page), ("a.mhtml", mhtml), ("a.webarchive", webarchive)):
        n = notice.from_pages([((name,), data)])
        assert n.buyer == "Påhittade Fastigheter", name
        assert n.org_number == "2120009999"
        assert n.reference == "X-7700"
        assert n.deadline == datetime(2027, 1, 15, 12, 0)
        assert n.cpv == ["45000000"] and n.procedure == "simplified"


def test_cp1252_page_and_listed_documents() -> None:
    page = notice_page("Örjan Exempelsson region", "232100-0004", "RS 1", "2026-01-01", "2026-02-01", "Svar nr 1.pdf")
    page = page.replace(b'charset="utf-8"', b'charset="windows-1252"').decode().encode("cp1252")
    n = notice.from_pages([(("Annons.htm",), page)])
    assert n.buyer == "Örjan Exempelsson region"
    assert notice.listed_stem("Svar nr 1.pdf") in n.listed
    assert notice.listed_stem("AF.pdf") not in n.listed  # listed under "Dokument", not under the Q&A heading
    assert "1" not in n.listed  # never a fragment of a name


def test_a_damaged_zip_gives_an_empty_notice(tmp_path: Path) -> None:
    path = tmp_path / "trasig.zip"
    path.write_bytes(b"PK\x03\x04 not really")
    assert notice.from_zip(path).found() == dict.fromkeys(notice.Notice().found(), False)


def test_a_page_titled_as_questions_and_answers_is_held_whatever_its_name(data_root: Path) -> None:
    incoming = data_root / "incoming" / "co-002"
    incoming.mkdir(parents=True)
    name = next(n for n, s in expected().items() if s == sh.DEV)
    meta = TENDERS[name]
    assert meta is not None
    page = "<html><head><title>Frågor och svar</title></head><body><p>Svar</p></body></html>"
    (incoming / name).write_bytes(zip_bytes({"Annons.html": notice_page(*meta), "sida2.html": page}))
    intake("co-002", incoming, new_ledger=True, now=NOW)
    inbox = paths.dev_tender_dir("co-002", "t-0001") / "inbox"
    assert sorted(p.name for p in inbox.iterdir()) == ["Annons.html"]
