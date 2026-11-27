"""Coverage by code group: every AMA or AF code group in the technical description and the AF maps to a calc post or to
a line set aside with a reason, and the calc lists the topmost groups that don't, by code number only. Every heading
here is invented ("Påhittad rubrik"); no AMA or AF title is used."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import openpyxl
import pytest
from kalkyl_synthetic import BLOCKS, DOC_A, DOC_B, OUTLINE, fake_sdk, first_block, good_handler, submit, write_catalog
from kalkyl_synthetic import message as kalkyl_message
from kalkyl_synthetic import write_prep as write_kalkyl_prep

from anbud import paths
from anbud.granskning.corpus import Corpus
from anbud.kalkyl.code_groups import coverage, group_code, parent
from anbud.models import Block

DOC_C = "cccccccccccc"  # the AF
DOC_D = "dddddddddddd"  # the general description: not checked


@pytest.mark.parametrize(
    ("title", "code"),
    [
        ("AFB.1 Påhittad rubrik", "AFB.1"),
        ("AFC.111 Påhittad rubrik", "AFC.111"),
        ("AFD Påhittad rubrik", "AFD"),
        ("63.CB Påhittad rubrik", "63.CB"),
        ("63.CBB/1 Påhittad rubrik", "63.CBB/1"),
        ("YGB.12 Påhittad rubrik", "YGB.12"),
        ("EL ALLMÄNT", None),  # an upper-case word is no code
        ("TILL ELARBETEN", None),
        ("YGB Påhittad rubrik", None),  # a letter code without a number reads like a word: its sub-codes still count
        ("2.1 Påhittad rubrik", None),  # plain numbering
        ("Påhittad rubrik", None),
    ],
)
def test_a_heading_code_is_an_af_code_a_digits_and_letters_code_or_a_letter_code_with_a_number(
    title: str, code: str | None
) -> None:
    assert group_code(title) == code


def test_the_hierarchy_comes_from_the_codes() -> None:
    def chain(code: str) -> list[str]:
        out = []
        while (code := parent(code)) is not None:
            out.append(code)
        return out

    assert chain("AFC.111") == ["AFC.11", "AFC.1", "AFC"]
    assert chain("63.CBB/1") == ["63.CBB", "63.CB", "63.C"]
    assert chain("YGB.12") == ["YGB.1", "YGB", "YG"]


def _b(doc: str, n: int, kind: str, text: str, page: int = 1) -> Block:
    return Block(id=f"{doc[:6]}-{n:06d}", doc_id=doc, kind=kind, text=text, page=page)  # type: ignore[arg-type]


# The synthetic tender's electrical description gets codes on its headings and a few more groups after its own text.
CODED_A = {1: "63.B Påhittad rubrik", 3: "63.C Påhittad rubrik", 9: "63.D Påhittad rubrik"}
EXTRA_A = [
    (13, "heading", "63.E Påhittad rubrik", 1),
    (14, "paragraph", "Kanalisation förläggs dolt i alla bostadsrum.", None),
    (15, "heading", "63.EB Påhittad rubrik", 2),
    (16, "paragraph", "Rören utförs enligt ritningen.", None),
    (17, "heading", "63.EC Påhittad rubrik", 2),
    (18, "paragraph", "Dosor monteras där ritningen anger.", None),
    (19, "heading", "YGB.1 Påhittad rubrik", 1),
    (20, "paragraph", "Kablar märks i båda ändar.", None),
    (21, "heading", "EL ALLMÄNT", 1),
    (22, "paragraph", "Text under en rubrik utan kod.", None),
]
AF_DOC = [
    (1, "heading", "AFB.1 Påhittad rubrik", 1),
    (2, "paragraph", "Anbud lämnas senast ett påhittat datum.", None),
    (3, "heading", "AFB.2 Påhittad rubrik", 1),
    (4, "paragraph", "Anbudet gäller en påhittad tid.", None),
    (5, "heading", "AFC.1 Påhittad rubrik", 1),
    (6, "paragraph", "Arbetsledare 1 st på plats under hela entreprenaden.", None),
]
GENERAL_DOC = [(1, "heading", "BEC.1 Påhittad rubrik", 1), (2, "paragraph", "Allmän text om huset.", None)]


def write_coded_prep(prep_dir: Path) -> Path:
    blocks = [
        b.model_copy(update={"text": CODED_A[int(b.id[-6:])]}) if b.doc_id == DOC_A and int(b.id[-6:]) in CODED_A else b
        for b in BLOCKS
    ]
    outline = [
        {**o, "title": CODED_A[int(o["block_id"][-6:])], "number": None} if o["doc_id"] == DOC_A else o for o in OUTLINE
    ]
    for doc, rows in ((DOC_A, EXTRA_A), (DOC_C, AF_DOC), (DOC_D, GENERAL_DOC)):
        for n, kind, text, level in rows:
            blocks.append(_b(doc, n, kind, text))
            if level is not None:
                outline.append(
                    {"doc_id": doc, "level": level, "title": text, "block_id": f"{doc[:6]}-{n:06d}", "page": 1}
                )
    write_kalkyl_prep(prep_dir, blocks, outline)
    names = {
        DOC_A: "El/E-63-1-001 Teknisk beskrivning el.pdf",
        DOC_B: "Lägenhetsförteckning.xlsx",
        DOC_C: "Förfrågningsunderlag/AF.pdf",
        DOC_D: "Förfrågningsunderlag/Rambeskrivning.pdf",
    }
    docs = [
        {"doc_id": d, "kind": "pdf", "paths": [name], "pages": [], "blocks": sum(b.doc_id == d for b in blocks)}
        for d, name in names.items()
    ]
    (prep_dir / "documents.json").write_text(json.dumps(docs))
    return prep_dir


def test_groups_roll_up_by_code_and_only_the_topmost_uncovered_ones_are_listed(tmp_path: Path) -> None:
    corpus = Corpus.load(write_coded_prep(tmp_path / "prep"))
    a, c = (lambda n: f"{DOC_A[:6]}-{n:06d}"), (lambda n: f"{DOC_C[:6]}-{n:06d}")
    cov = coverage(corpus, posted={a(4), a(10)}, reasoned={c(6)}, lines={a(4), a(10), c(6), a(14)}, open_lines={a(14)})
    assert sorted(cov.codes) == ["63.B", "63.C", "63.D", "63.E", "63.EB", "63.EC", "AFB.1", "AFB.2", "AFC.1", "YGB.1"]
    rows = [(g.code, g.codes, g.blocks, g.lines, g.open_lines, g.block_id) for g in cov.missing]
    assert rows == [
        ("63.B", 1, 1, 0, 0, a(1)),
        ("63.E", 3, 3, 1, 1, a(13)),  # its two sub-codes are one row, not three
        ("YGB.1", 1, 1, 0, 0, a(19)),  # no YGB heading: the group with one code is that code
        ("AFB", 2, 2, 0, 0, c(1)),  # no AFB heading: AFB.1 and AFB.2 group under their code
    ]
    assert cov.summary() == {
        "teknisk": {"codes": 7, "with_post": 2, "with_reason_only": 0, "without": 5, "rows": 3},
        "af": {"codes": 3, "with_post": 0, "with_reason_only": 1, "without": 2, "rows": 1},
    }
    assert not any(g.code.startswith("BEC") for g in cov.codes.values())  # the general description isn't checked


def test_a_sub_code_with_a_post_covers_its_parent_but_not_its_siblings(tmp_path: Path) -> None:
    corpus = Corpus.load(write_coded_prep(tmp_path / "prep"))
    cov = coverage(corpus, posted={f"{DOC_A[:6]}-000016"}, reasoned=set(), lines=set(), open_lines=set())
    assert cov.codes["63.E"].post and cov.codes["63.EB"].post and not cov.codes["63.EC"].covered
    assert "63.EC" in [g.code for g in cov.missing] and "63.E" not in [g.code for g in cov.missing]


def test_a_tender_without_coded_headings_is_not_checked(tmp_path: Path) -> None:
    corpus = Corpus.load(write_kalkyl_prep(tmp_path / "prep"))  # numbered headings only
    cov = coverage(corpus, posted=set(), reasoned=set(), lines=set(), open_lines=set())
    assert cov.codes == {} and cov.missing == []


def _handler(params: dict[str, Any]) -> Any:
    """The good answers, and the AF's site manager line set aside with a reason."""
    if (
        "tools" in params
        and params["tools"][0]["name"] == "submit_quantities"
        and first_block(params) == "cccccc-000001"
    ):
        aside = {"block_ids": ["cccccc-000006"], "kind": "no_requirement", "reason": "an organisation requirement"}
        return kalkyl_message([submit({"records": [], "set_aside": [aside]})])
    return good_handler(params)


def test_the_calc_lists_the_groups_without_a_post_or_a_reason_by_code_only(data_root: Path) -> None:
    from anbud.kalkyl.run import run_dev_tender

    tender = paths.dev_tender_dir("co-001", "t-0001")
    write_coded_prep(tender / "prep")
    write_catalog(data_root)
    summary = run_dev_tender("co-001", "t-0001", sdk=fake_sdk(_handler), catalog_name="synthetic", run_id="k-codes")
    assert summary["code_groups"]["teknisk"]["rows"] == 3 and summary["code_groups"]["af"]["with_reason_only"] == 1
    wb = openpyxl.load_workbook(tender / "kalkyl" / "k-codes" / "kalkyl.xlsx")
    sheet = [[c.value for c in r] for r in wb["Kodgrupper"].iter_rows(min_row=5)]
    assert [r[0] for r in sheet] == ["63.B", "63.E", "YGB.1", "AFB"]  # the technical description first, then the AF
    assert sheet[1][:7] == ["63.E", "Teknisk beskrivning", 3, DOC_A, "aaaaaa-000013", 1, 3]
    text = json.dumps(sheet, ensure_ascii=False)
    assert "Påhittad rubrik" not in text and "Kanalisation" not in text  # code numbers and block ids, never words
    over = {r[0].value: r[1] for r in wb["Översikt"].iter_rows(min_row=5)}
    assert over["Kodgrupper utan post eller skäl (se Kodgrupper)"].value == 4
    assert over["Kodgrupper utan post eller skäl (se Kodgrupper)"].fill.fgColor.rgb.endswith("FCE4D6")
    assert over["Kodgrupper (AMA/AF) i tekniska beskrivningen och AF"].value == (
        "Teknisk beskrivning: 7 koder, 2 med post, 0 med skäl, 5 utan; AF: 3 koder, 0 med post, 1 med skäl, 2 utan"
    )


def test_a_calc_without_coded_headings_says_not_checked(data_root: Path) -> None:
    from anbud.kalkyl.run import run_dev_tender

    tender = paths.dev_tender_dir("co-001", "t-0001")
    write_kalkyl_prep(tender / "prep")
    write_catalog(data_root)
    run_dev_tender("co-001", "t-0001", sdk=fake_sdk(), catalog_name="synthetic", run_id="k-plain")
    wb = openpyxl.load_workbook(tender / "kalkyl" / "k-plain" / "kalkyl.xlsx")
    note = "Inga kodade rubriker (AMA/AF) i tekniska beskrivningen eller AF: inte kontrollerat"
    assert wb["Kodgrupper"]["A5"].value == note
    over = {r[0].value: r[1].value for r in wb["Översikt"].iter_rows(min_row=5)}
    assert over["Kodgrupper (AMA/AF) i tekniska beskrivningen och AF"] == note
    assert over["Kodgrupper utan post eller skäl (se Kodgrupper)"] == 0
