"""Other trades' documents leave the models' input, fail-open, and stay searchable; the lines in them that refer to the
electrical work come back with their headings. Synthetic documents only."""

from __future__ import annotations

import pytest
from mixed_trades import EL, UNKNOWN, VS, A, K, block_id, write_mixed_prep

from anbud.granskning.corpus import Corpus
from anbud.granskning.prefix import EXCLUDED_NOTE, corpus_text
from anbud.granskning.rank import coverage
from anbud.granskning.search import SearchIndex
from anbud.granskning.toolbox import Toolbox
from anbud.selection import (
    DocumentView,
    SelectionError,
    apply,
    classify,
    excluded_counts,
    excluded_label,
    load_selected,
    select,
)


@pytest.mark.parametrize(
    ("view", "discipline", "trade"),
    [
        (DocumentView("d", ("Handlingar/El/E-63-1-001.pdf",)), "el", None),
        (DocumentView("d", ("Belysningsplan plan 2.pdf",)), "el", None),
        (DocumentView("d", ("T-64-1-001 Telesystem.pdf",)), "el", None),
        (DocumentView("d", ("VS/R-50-1-001.pdf",)), "other", "VS"),
        (DocumentView("d", ("Teknisk beskrivning VS-installationer.pdf",)), "other", "VS"),
        (DocumentView("d", ("Mängdförteckning VS.pdf",)), "other", "VS"),
        (DocumentView("d", ("Ventilationsritningar/plan 2.pdf",)), "other", "V"),
        (DocumentView("d", ("K20-1-001.pdf",)), "other", "K"),
        (DocumentView("d", ("A-40-1-001 Planritning.pdf",)), "other", "A"),
        (DocumentView("d", ("AF.pdf",)), "common", None),
        (DocumentView("d", ("Administrativa föreskrifter.docx",)), "common", None),
        (DocumentView("d", ("Gränsdragningslista.xlsx",)), "common", None),
        (DocumentView("d", ("Gränsdragningslista El-VS.pdf",)), "el", None),  # electrical and another trade: read
        (DocumentView("d", ("Mängdförteckning.pdf",)), "unknown", None),
        (DocumentView("d", ("Bilaga 7.pdf",)), "unknown", None),
        # A project name in a folder is no trade word: "Markvägen" is no groundworks document.
        (DocumentView("d", ("Markvägen 3/Bilaga 4.pdf",)), "unknown", None),
        (DocumentView("d", ("Markvägen 3/Markritningar/M-10-1-001.pdf",)), "other", "M"),
        # The first page's opening text and the code headings count too.
        (DocumentView("d", ("Bilaga 9.pdf",), header="Teknisk beskrivning Ventilationsinstallationer"), "other", "V"),
        (DocumentView("d", ("Bilaga 9.pdf",), heading_codes=("57.B", "57.C", "53.F")), "other", "VS"),
        (DocumentView("d", ("Bilaga 9.pdf",), heading_codes=("63.B", "63.C", "63.F")), "el", None),
        (DocumentView("d", ("Bilaga 9.pdf",), heading_codes=("AFA.1", "AFB.2", "AFC.11")), "common", None),
        (DocumentView("d", ("Bilaga 9.pdf",), heading_codes=("57.B", "63.C")), "unknown", None),  # too few codes
    ],
)
def test_a_documents_discipline_is_read_from_generic_signals(view: DocumentView, discipline: str, trade: str) -> None:
    c = classify(view)
    assert (c.discipline, c.trade) == (discipline, trade)
    assert c.read is (discipline != "other")


def test_signals_name_kinds_never_a_file_name() -> None:
    c = classify(DocumentView("d", ("Kv Påhittet/VS/R-50-1-001 Teknisk beskrivning VS.pdf",)))
    assert c.signals == ("name:R", "name:VS", "folder:VS")


@pytest.fixture
def corpus(tmp_path):
    return Corpus.load(write_mixed_prep(tmp_path / "t" / "prep"))


def test_other_trades_leave_the_input_and_their_electrical_lines_come_back(corpus: Corpus) -> None:
    sel = select(corpus)
    assert sel.excluded == {A: "A", K: "K", VS: "VS"}  # the architect too, since the tender has electrical documents
    assert {d: c.discipline for d, c in sel.classes.items()}[UNKNOWN] == "unknown"  # read
    # The line that names the electrical contractor comes back with its heading; the plumbing lines don't.
    assert sel.boundary == (block_id(VS, 3), block_id(VS, 4)) and sel.boundary_lines == 1
    assert sel.pages_excluded == 40 + 25 + 12 and sel.excluded_pages_by_trade() == {"A": 12, "K": 25, "VS": 40}

    read = apply(corpus, sel)
    assert [b for b in read.order if read.blocks[b].doc_id == VS] == [block_id(VS, 3), block_id(VS, 4)]
    assert not any(read.blocks[b].doc_id in (A, K) for b in read.order)
    assert read.all_order == corpus.order and read.blocks == corpus.blocks  # every block stays reachable
    assert [b.id for b in read.neighbours(block_id(VS, 2))] == [block_id(VS, n) for n in (1, 2, 3)]
    hits = SearchIndex(read).search("isolering")
    assert [h.block_id for h in hits] == [block_id(VS, 2)]  # searchable though not read
    assert excluded_label(read) == "77 sidor, 3 dokument (A 12, K 25, VS 40)"
    assert excluded_counts(read)["boundary_lines"] == 1


def test_the_prefix_marks_excluded_documents_and_holds_only_their_electrical_lines(corpus: Corpus) -> None:
    read = apply(corpus, select(corpus))
    text = corpus_text(read)
    assert f'{VS} pdf "R-50-1-001 Teknisk beskrivning VS.pdf" pages=40 blocks=5 excluded="VS"' in text
    assert EXCLUDED_NOTE in text
    assert "Cirkulationspumpar elansluts av elentreprenören." in text and "isoleras" not in text
    assert "håltagningar" not in text and "Varje lägenhet förses" in text and "underentreprenad" in text
    out = Toolbox(read, []).run("search_blocks", {"query": "håltagningar"})
    assert f"{block_id(K, 1)}" in out.content and "[excluded: K]" in out.content


def test_a_tender_with_nothing_excluded_gets_the_same_prefix_bytes(tmp_path) -> None:
    from kalkyl_synthetic import write_prep

    plain = Corpus.load(write_prep(tmp_path / "prep"))
    sel = select(plain)
    assert not sel.excluded and corpus_text(apply(plain, sel)) == corpus_text(plain)


def test_the_architect_stays_without_electrical_documents_and_an_all_other_tender_is_read_whole(tmp_path) -> None:
    from mixed_trades import DOCS

    no_el = Corpus.load(write_mixed_prep(tmp_path / "a" / "prep", [d for d in DOCS if d[0] != EL]))
    sel = select(no_el)
    assert A not in sel.excluded and VS in sel.excluded  # the architect's plans are what the work is counted from
    only_other = Corpus.load(write_mixed_prep(tmp_path / "b" / "prep", [d for d in DOCS if d[0] in (VS, K)]))
    assert select(only_other).excluded == {}  # every document looks like another trade's: the signals are wrong


def test_the_override_moves_documents_and_refuses_what_it_cant_place(corpus: Corpus, tmp_path) -> None:
    override = tmp_path / "selection.toml"
    override.write_text(f'read = ["{VS}"]\nexclude = ["{UNKNOWN}"]\n')
    sel = select(corpus, override)
    assert VS not in sel.excluded and sel.excluded[UNKNOWN] == "manuellt" and sel.overridden == (UNKNOWN, VS)
    assert sel.override_sha256 and sel.summary()["overridden"] == 2
    override.write_text('read = ["nosuchdoc0000"]\n')
    with pytest.raises(SelectionError, match="1 unknown document ids"):
        select(corpus, override)
    override.write_text(f'read = ["{VS}"]\nexclude = ["{VS}"]\n')
    with pytest.raises(SelectionError, match="both reads and excludes"):
        select(corpus, override)


def test_load_selected_reads_the_tender_folders_override(tmp_path) -> None:
    prep = write_mixed_prep(tmp_path / "t" / "prep")
    (prep.parent / "selection.toml").write_text(f'read = ["{K}"]\n')
    read, sel = load_selected(prep)
    assert K not in read.excluded and K not in sel.excluded and VS in read.excluded


def test_the_review_coverage_names_what_the_models_didnt_read(corpus: Corpus) -> None:
    read = apply(corpus, select(corpus))
    cov = coverage([], read)
    assert cov.other_discipline == "77 sidor, 3 dokument (A 12, K 25, VS 40)" and cov.boundary_lines == 1
    assert "R-50-1-001 Teknisk beskrivning VS.pdf (VS, 40 s.)" in cov.other_discipline_documents


def test_kalkyl_reads_only_the_selection_and_reports_what_it_left_out(data_root) -> None:
    import json

    import openpyxl
    from kalkyl_synthetic import fake_sdk, write_catalog

    from anbud import paths
    from anbud.kalkyl.run import run_dev_tender

    tender = paths.dev_tender_dir("co-001", "t-0001")
    write_mixed_prep(tender / "prep")
    write_catalog(data_root)
    sdk = fake_sdk()
    summary = run_dev_tender("co-001", "t-0001", sdk=sdk, catalog_name="synthetic", run_id="kalkyl-s")
    assert summary["selection"]["documents_excluded"] == 3 and summary["selection"]["pages_excluded"] == 77
    assert summary["selection"]["boundary_lines"] == 1

    sent = "\n".join(str(p["messages"]) for p in sdk.messages.sent)
    assert block_id(VS, 4) in sent and block_id(EL, 2) in sent  # the electrical line from the plumbing description
    assert not any(block_id(d, 1) in sent for d in (K, A)) and block_id(VS, 2) not in sent

    out = tender / "kalkyl" / "kalkyl-s"
    result = json.loads((out / "result.json").read_text())
    uncovered = {u["block_id"] for u in result["uncovered"]}
    assert block_id(VS, 2) not in uncovered and block_id(K, 1) not in uncovered  # not counted as not handled
    assert block_id(EL, 3) in uncovered  # an electrical quantity line nobody handled still is
    saved = json.loads((out / "selection.json").read_text())
    assert {d["doc_id"]: d["read"] for d in saved["documents"]}[VS] is False and saved["boundary"]
    rows = {r[0].value: r for r in openpyxl.load_workbook(out / "kalkyl.xlsx")["Översikt"].iter_rows(min_row=5)}
    assert rows["Ej lästa (annan disciplin)"][1].value == "77 sidor, 3 dokument (A 12, K 25, VS 40)"
    assert rows["Rader i andra discipliners handlingar som rör elarbetena (lästa)"][1].value == 1
