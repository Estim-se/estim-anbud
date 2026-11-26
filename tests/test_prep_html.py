"""Saved notice pages: kind detection, blocks, and the answer key withheld. Every page here is invented."""

from __future__ import annotations

import io
import json
import plistlib
import zipfile
from email.message import EmailMessage
from pathlib import Path

import pytest
from conftest import assert_no_fake_names
from test_prep_tender import inbox, text_pdf
from test_runguard import ledger_with

from anbud import cli
from anbud.evals import split_heldout as sh
from anbud.granskning.corpus import Corpus
from anbud.prep import html as html_reader
from anbud.prep import tender
from anbud.prep.build import DocBuilder
from anbud.prep.html import read_html
from anbud.prep.sources import detect_kind

SHA = "ab" * 32


def read(page: str | bytes) -> tuple[DocBuilder, int]:
    builder = DocBuilder(SHA, SHA[:12])
    withheld = read_html(page.encode() if isinstance(page, str) else page, builder)
    return builder, withheld


def texts(builder: DocBuilder) -> list[str]:
    return [b.text for b in builder.blocks]


def page(body: str, head: str = "<title>Annons</title>") -> str:
    return f"<!DOCTYPE html><html><head>{head}</head><body>{body}</body></html>"


SECRET_QA = "Svaret är att kabelstegen ingår i entreprenaden."
SECRET_AMENDMENT = "Ny ritning ersätter den tidigare utgåvan."
NOTICE = page(
    "<h1>1 Annons</h1><p>Upphandlingen gäller en ombyggnad.</p>"
    "<h2>Anbudsförfrågan</h2><p>Sista anbudsdag framgår nedan.</p>"
    f"<h2>Frågor och svar</h2><h3>Fråga 1</h3><p>{SECRET_QA}</p>"
    "<p>Kontakt: fraga.svar@example.se. Handlingen är HEMLIG enligt beslut från myndigheten.</p>"
    f"<h2>Ändring 1</h2><p>{SECRET_AMENDMENT}</p>"
    "<h2>Dokument</h2><p>Förfrågningsunderlaget finns som bilagor.</p>"
)


# -- kind detection


def zipped(name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, "<x/>")
    return buf.getvalue()


def mhtml(html: str, charset: str = "utf-8") -> bytes:
    message = EmailMessage()
    message["MIME-Version"] = "1.0"
    message["Subject"] = "Annons"
    message.set_type("multipart/related")
    part = EmailMessage()
    part.set_content(html.encode(charset), maintype="text", subtype="html", cte="quoted-printable")
    part.set_param("charset", charset)
    message.attach(part)
    image = EmailMessage()
    image.set_content(b"\x89PNG\r\n\x1a\n", maintype="image", subtype="png")
    message.attach(image)
    return message.as_bytes()


def webarchive(html: bytes, encoding: str | None = "UTF-8") -> bytes:
    main = {"WebResourceData": html, "WebResourceMIMEType": "text/html", "WebResourceURL": "https://example.se/"}
    if encoding:
        main["WebResourceTextEncodingName"] = encoding
    return plistlib.dumps({"WebMainResource": main}, fmt=plistlib.FMT_BINARY)


def test_web_pages_are_detected_by_content_or_by_name() -> None:
    assert detect_kind(b"\xef\xbb\xbf  \n<!DOCTYPE HTML>\n<html>", "x.bin") == "html"
    assert detect_kind(b"<HTML><body>hej</body></HTML>", "x") == "html"
    assert detect_kind(b"<!-- saved from url=(0014)about:internet -->\n<html>", "x") == "html"
    assert detect_kind(b'<?xml version="1.0"?>\n<!DOCTYPE html PUBLIC "x">\n<html xmlns="x">', "x") == "html"
    assert detect_kind("<!DOCTYPE html>".encode("utf-16"), "x") == "html"
    assert detect_kind(b"<div>a fragment</div>", "page.xhtml") == "html"
    assert detect_kind(b"<div>a fragment</div>", "page.HTM") == "html"
    assert detect_kind(b"<div>\x00</div>", "page.html") == "other"
    assert detect_kind(b"<div>a fragment</div>", "page.txt") == "text"
    assert detect_kind(b"<html><body>notes</body></html>", "notes.txt") == "text"
    assert detect_kind(b"<!DOCTYPE html><p>x</p>", "notes.md") == "text"
    assert detect_kind(mhtml("<p>x</p>"), "x") == "html"
    assert detect_kind(b"anything", "saved.mht") == "html"
    assert detect_kind(webarchive(b"<p>x</p>"), "saved.webarchive") == "html"
    assert detect_kind(webarchive(b"<p>x</p>"), "x.plist") == "other"


def test_other_kinds_are_detected_as_before() -> None:
    assert detect_kind(b"%PDF-1.7 <html>", "x.html") == "pdf"
    assert detect_kind(zipped("xl/workbook.xml"), "x.html") == "xlsx"
    assert detect_kind(zipped("word/document.xml"), "x.htm") == "docx"
    assert detect_kind(zipped("readme.txt"), "x.html") == "archive"
    assert detect_kind(b"\x89PNG\r\n\x1a\n....", "x.html") == "image"
    assert detect_kind(b"hej", "notes.txt") == "text"


# -- blocks


def test_headings_paragraphs_and_table_rows() -> None:
    builder, withheld = read(
        page(
            "<h1>Annons</h1><p>Detta är <b>en</b> <a href='#'>text</a><br>med radbrytning.</p>"
            "<h2>2.1 Omfattning</h2><ul><li>Ett<li>Två</ul><div>Direkt text i en div.</div>"
            "<table><tr><th>Post</th><th>Mängd</th></tr><tr><td>Kabel</td><td><p>12</p> m</td></tr></table>"
            "<h3>Krav</h3><dl><dt>Start</dt><dd>Vecka 10</dd></dl>"
        )
    )
    assert withheld == 0
    assert texts(builder) == [
        "Annons",
        "Detta är en text med radbrytning.",
        "2.1 Omfattning",
        "Ett",
        "Två",
        "Direkt text i en div.",
        "Post | Mängd",
        "Kabel | 12 m",
        "Krav",
        "Start",
        "Vecka 10",
    ]
    kinds = [b.kind for b in builder.blocks]
    assert kinds.count("heading") == 3 and kinds.count("table_row") == 2
    assert [b.line for b in builder.blocks] == list(range(1, 12))
    assert [(o.level, o.title, o.number) for o in builder.outline] == [
        (1, "Annons", None),
        (2, "2.1 Omfattning", "2.1"),
        (3, "Krav", None),
    ]


def test_scripts_styles_navigation_and_controls_vanish() -> None:
    builder, _ = read(
        page(
            "<nav><a>Startsida</a></nav><script>var dold = 'skript';</script><style>p { color: red }</style>"
            "<noscript>Aktivera skript</noscript><button>Skicka</button><select><option>Val</option></select>"
            "<textarea>Fri text</textarea><iframe>ram</iframe><svg><text>Figur</text></svg><p>Kvar.</p>",
            head="<title>Annons</title><style>body {}</style><script>x()</script>",
        )
    )
    assert texts(builder) == ["Annons", "Kvar."]  # the title stands in for the missing h1


def test_the_title_is_not_used_when_the_page_has_an_h1() -> None:
    builder, _ = read(page("<h1>Rubrik</h1><p>Text.</p>"))
    assert texts(builder) == ["Rubrik", "Text."]


def test_a_layout_table_is_read_as_blocks() -> None:
    builder, _ = read(page("<table><tr><td><h1>Rubrik</h1><p>Första.</p><p>Andra.</p></td></tr></table>"))
    assert texts(builder) == ["Rubrik", "Första.", "Andra."]
    assert builder.outline[0].level == 1


def test_charset_from_meta_decodes_swedish_letters() -> None:
    html = page("<h1>Åtgärder för väggar</h1><p>Öppning i fasad.</p>", head='<meta charset="windows-1252">')
    builder, _ = read(html.encode("cp1252"))
    assert texts(builder) == ["Åtgärder för väggar", "Öppning i fasad."]
    http_equiv = '<meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1">'
    builder, _ = read(page("<p>Värme</p>", head=http_equiv).encode("cp1252"))
    assert texts(builder) == ["Värme"]
    builder, _ = read(page("<p>Värme</p>", head="").encode("cp1252"))  # no declaration, not UTF-8
    assert texts(builder) == ["Värme"]


def test_mhtml_and_webarchive_pages_are_read() -> None:
    html = page("<h1>Åtgärd</h1><p>Text i arkivet.</p>", head="")
    builder, _ = read(mhtml(html, "iso-8859-1"))
    assert texts(builder) == ["Åtgärd", "Text i arkivet."]
    builder, _ = read(webarchive(html.encode("cp1252"), "windows-1252"))
    assert texts(builder) == ["Åtgärd", "Text i arkivet."]


def test_hidden_elements_become_hidden_blocks() -> None:
    builder, _ = read(
        page(
            "<h1>A</h1><p>Synlig <span style='display: none'>dold</span> text.</p>"
            "<div hidden><p>Gömd.</p></div><p aria-hidden='true'>Aria.</p>"
            "<table><tr style='visibility:hidden'><td>Cell</td></tr></table>"
        )
    )
    hidden = {b.text for b in builder.blocks if b.hidden}
    assert hidden == {"dold", "Gömd.", "Aria.", "Cell"}
    assert {"Synlig", "text."} <= {b.text for b in builder.blocks if not b.hidden}


# -- the answer key


def test_answer_sections_are_withheld_up_to_the_next_heading_of_their_level() -> None:
    builder, withheld = read(NOTICE)
    assert withheld == 2
    assert texts(builder) == [
        "1 Annons",
        "Upphandlingen gäller en ombyggnad.",
        "Anbudsförfrågan",
        "Sista anbudsdag framgår nedan.",
        "Dokument",
        "Förfrågningsunderlaget finns som bilagor.",
    ]
    seen = json.dumps([*[b.model_dump() for b in builder.blocks], *[o.model_dump() for o in builder.outline]])
    for secret in (SECRET_QA, SECRET_AMENDMENT, "Frågor och svar", "Ändring 1", "Fråga 1"):
        assert secret not in seen
    assert builder.masked == [] and builder.markings == []


def test_a_section_element_ends_its_withheld_section() -> None:
    builder, withheld = read(
        page("<h1>A</h1><section><h3>Frågor och svar</h3><p>Dolt svar.</p></section><p>Efter sektionen.</p>")
    )
    assert withheld == 1 and texts(builder) == ["A", "Efter sektionen."]


def test_labelled_elements_and_summaries_are_withheld_whole() -> None:
    builder, withheld = read(
        page(
            "<h1>A</h1><div class='panel question-list'><p>Fråga om kabel.</p><div><p>Svar om kabel.</p></div></div>"
            "<div id='fragor-och-svar'><p>Ännu ett svar.</p></div><p data-tab='Tilldelningsbeslut'>Vinnare.</p>"
            "<details><summary>Rättelse</summary><p>Rättad text.</p></details>"
            "<details><summary>Bilagor</summary><p>Ritningar.</p></details><p>Slut.</p>"
        )
    )
    assert withheld == 4
    assert texts(builder) == ["A", "Bilagor", "Ritningar.", "Slut."]


def test_a_page_titled_as_an_answer_with_no_h1_is_withheld_whole() -> None:
    builder, withheld = read(page("<h2>Fråga 1</h2><p>Svar.</p><p>Mer.</p>", head="<title>Frågor och svar</title>"))
    assert builder.blocks == [] and withheld == 2  # the h2 section, then the whole page


def test_deep_nesting_and_broken_markup_do_not_crash() -> None:
    builder, withheld = read(page("<div>" * 5000 + "Djup text." + "</div>" * 5000 + "<p>Efter.</p>"))
    assert texts(builder) == ["Annons", "Efter."] and withheld == 1  # text past the cap can't be checked
    builder, _ = read("<html><body><p>Öppen <b>fet <i>kursiv<p>Nästa &bogus; &amp &#99999999; <td>cell")
    assert builder.blocks and all(b.text for b in builder.blocks)
    builder, _ = read(f"<p title='{'x' * 1_000_000}'>Lång attribut.</p>")
    assert texts(builder) == ["Lång attribut."]


def test_a_withheld_element_is_not_closed_early_past_the_depth_cap() -> None:
    nested = "<div class='question-list'>" + "<div>" * 5000 + "</div>" * 1200 + "Dolt svar." + "</div>" * 3801
    builder, withheld = read(page(f"<h1>A</h1>{nested}<p>Efter.</p>"))
    assert withheld == 1 and "Dolt svar." not in texts(builder)


def test_markup_the_parser_rejects_is_unreadable(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def reject(self: object, data: str) -> None:
        raise AssertionError("unexpected char in declaration")  # how the standard parser reports bad declarations

    monkeypatch.setattr(html_reader._Page, "feed", reject)
    folder = inbox(data_root, {"a.html": NOTICE.encode(), "b.webarchive": b"bplist00 damaged"})
    summary = tender.prepare("co-001", folder, folder.parent / "prep", root=data_root)
    docs = json.loads((folder.parent / "prep" / "documents.json").read_text())
    assert [d["kind"] for d in docs] == ["html", "html"]
    assert {d["skipped"] for d in docs} == {"unreadable, damaged or password-protected"}
    assert summary["withheld_sections"] == 0 and summary["block_chars"] == 0


def test_a_heading_that_is_never_shown_does_not_end_a_withheld_section() -> None:
    for inner in ("<nav><h2>Meny</h2></nav>", "<template><h2>Mall</h2></template>", "<h2></h2>", "<h2> <br> </h2>"):
        builder, withheld = read(
            page(
                f"<h1>A</h1><h2>Frågor och svar</h2><p>Svar ett.</p>{inner}<p>Svar två.</p>"
                "<h2>Dokument</h2><p>Kvar.</p>"
            )
        )
        assert withheld == 1 and texts(builder) == ["A", "Dokument", "Kvar."], inner


def test_a_labelled_heading_that_ends_a_section_is_still_withheld() -> None:
    builder, withheld = read(
        page("<h1>A</h1><h2>Frågor och svar</h2><p>Svar.</p><h2 class='question-list'>Övrigt</h2><p>Mer svar.</p>")
    )
    assert withheld == 2 and texts(builder) == ["A"]


def test_a_summary_or_legend_in_a_table_cell_withholds_its_element() -> None:
    builder, withheld = read(
        page(
            "<h1>A</h1><table><tr><td>Före</td><td><details><summary>Frågor och svar</summary><p>Svar.</p></details>"
            "<fieldset><legend>Rättelse</legend><p>Rättat.</p></fieldset></td></tr><tr><td>Nästa rad</td></tr></table>"
        )
    )
    assert withheld == 2 and texts(builder) == ["A", "Före", "Nästa rad"]


def test_unclosed_inline_elements_past_the_depth_cap_keep_the_answer_key_withheld() -> None:
    for opener in ("<a name=x>", "<font size=2>", "<span>"):
        flood = opener * 1100
        builder, withheld = read(
            page(
                f"<h1>A</h1>{flood}<h2>Frågor och svar</h2><p>Svar.</p>"
                "<h2>Dokument</h2><p>Kvar.</p><div class='question-list'>Mer.</div>"
            )
        )
        assert withheld == 2 and texts(builder) == ["A", "Dokument", "Kvar."], opener


def test_unclosed_options_do_not_swallow_the_rest_of_the_page() -> None:
    builder, withheld = read(page("<h1>A</h1><select>" + "<option>Val" * 1100 + "</select><p>Efter listan.</p>"))
    assert withheld == 0 and texts(builder) == ["A", "Efter listan."]


def test_text_opened_past_the_depth_cap_is_withheld_and_counted() -> None:
    flood = "<div>" * 1100
    builder, withheld = read(page(f"<h1>A</h1>{flood}<h2>Frågor och svar</h2><p>Svar.</p>"))
    assert withheld == 1 and texts(builder) == ["A"]


def test_a_page_mostly_in_utf8_with_one_stray_byte_stays_utf8() -> None:
    body = "<h1>A</h1><h2>Ändring 1</h2><p>Dolt.</p><h2>Rättelse</h2><p>Dolt två.</p><h2>Övrigt</h2><p>Kvar.</p>"
    raw = page(body, head="").encode() + b"<p>Citat \x93fr\xe5n Word\x94.</p>"
    builder, withheld = read(raw)
    assert withheld == 2 and texts(builder)[:3] == ["A", "Övrigt", "Kvar."]


def test_a_head_left_open_does_not_swallow_the_body() -> None:
    builder, _ = read("<html><head><title>T</title><meta charset=utf-8><body><h1>Rubrik</h1><p>Text.</p>")
    assert texts(builder) == ["Rubrik", "Text."]
    builder, _ = read("<html><head><title>T</title><h1>Rubrik</h1><p>Text.</p>")
    assert texts(builder) == ["Rubrik", "Text."]
    builder, _ = read("<html><head><title>T</title>Lös text.")
    assert texts(builder) == ["T", "Lös text."]


def test_hidden_text_in_a_table_row_is_its_own_hidden_block() -> None:
    builder, _ = read(
        page(
            "<h1>A</h1><table><tr><td><span style='display:none'>20261126</span>26 nov</td><td>Synlig</td></tr></table>"
        )
    )
    rows = [(b.text, b.hidden) for b in builder.blocks if b.kind == "table_row"]
    assert rows == [("26 nov | Synlig", False), ("20261126", True)]


def test_a_charset_whose_codec_rejects_replacement_falls_back() -> None:
    builder, _ = read(page("<h1>Åtgärd</h1>", head="<meta charset='idna'>"))
    assert texts(builder) == ["Åtgärd"]


# -- end to end


def test_prepare_reads_a_notice_page_and_counts_what_it_withheld(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = inbox(
        data_root, {"annons.html": NOTICE.encode(), "beskrivning.pdf": text_pdf("1.1 Omfattning av arbetena")}
    )
    out = folder.parent / "prep"
    summary = tender.prepare("co-001", folder, out, root=data_root)
    assert summary["documents_by_kind"] == {"html": 1, "pdf": 1}
    assert summary["withheld_sections"] == 2 and summary["skipped"] == 0
    docs = {d["kind"]: d for d in json.loads((out / "documents.json").read_text())}
    assert docs["html"]["blocks"] == 6 and docs["html"]["withheld"] == 2 and docs["html"]["pages"] == []
    assert docs["html"]["tiles"] == 0
    written = "".join((out / name).read_text() for name in ("blocks.jsonl", "outline.json", "masking.jsonl"))
    for secret in (SECRET_QA, SECRET_AMENDMENT, "fraga.svar@example.se"):
        assert secret not in written
    assert tender.prepare("co-001", folder, out, root=data_root)["cache_hits"] == 2

    corpus = Corpus.load(out)
    assert {b.doc_id for b in corpus.blocks.values()} == {d["doc_id"] for d in docs.values()}

    ledger_with(data_root, t_0001=sh.DEV)
    assert cli.main(["prep", "co-001", "t-0001"]) == 0
    printed = capsys.readouterr()
    assert "withheld_sections" in printed.out and SECRET_QA not in printed.out + printed.err
    assert "annons" not in printed.out + printed.err
    assert_no_fake_names(printed.out, printed.err)
