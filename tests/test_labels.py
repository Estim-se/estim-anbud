"""`anbud export-review` and `anbud import-labels`, on the synthetic injection fixture in a temporary data root."""

from __future__ import annotations

import io
import json
from pathlib import Path

import openpyxl
import pytest

from anbud import paths
from anbud.cli import main
from anbud.granskning.contracts import fingerprint
from anbud.granskning.corpus import Corpus
from anbud.granskning.injection import AnswerKey, anchor_blocks
from anbud.granskning.labels import (
    HEADER,
    SHEET,
    TAXONOMY,
    LabelError,
    ReviewFlag,
    build_review,
    import_labels,
    read_review_flags,
)
from anbud.models import Citation, Flag, MoneyBasis
from anbud.prep.tender import FIXTURE_EXCLUDE, prepare

FIXTURE = paths.repo_root() / "fixtures" / "synthetic" / "injection-tender"
KEY = AnswerKey.load(FIXTURE / "expected.json")
COMPANY = "co-000"
EXPORT = "0a1b2c3d"
ACCEPT = next(t.text for t in TAXONOMY if t.code == "accept")
WRONG = next(t.text for t in TAXONOMY if t.code == "wrong_quote")


@pytest.fixture
def tender(data_root: Path) -> Path:
    folder = paths.fixture_data_dir(COMPANY, FIXTURE / "clean", data_root)
    prepare(COMPANY, FIXTURE / "clean", folder / "prep", root=data_root, exclude=FIXTURE_EXCLUDE)
    return folder


def _flag(corpus: Corpus, planted_id: str, lens: str) -> Flag:
    planted = next(p for p in KEY.planted if p.id == planted_id)
    cites = [Citation(block_id=anchor_blocks(corpus, a.text, a.file)[0].id, quote=a.text) for a in planted.anchors]
    return Flag(
        lens_id=lens,
        claim_type=planted.claim_type,
        claim=f"Påstående om {planted_id}.",
        citations=cites,
        money_basis=MoneyBasis(kind="missing_post"),
    )


def _items(corpus: Corpus) -> list[ReviewFlag]:
    shown = [
        ReviewFlag(flag=_flag(corpus, "P1", "conflicts"), verdict="confirmed"),
        ReviewFlag(flag=_flag(corpus, "P2", "realism"), verdict="partly"),
        ReviewFlag(flag=_flag(corpus, "P3", "trade_boundaries"), verdict="confirmed"),
    ]
    vite = anchor_blocks(corpus, "utgår vite med 15 000 kr")[0]
    refuted = Flag(
        lens_id="contract",
        claim_type="contract_vite",
        claim="Vitet saknar tak.",
        citations=[Citation(block_id=vite.id, quote="utgår vite med 15 000 kr")],
        money_basis=MoneyBasis(kind="contract_formula", formula="vite_per_week"),
    )
    return [*shown, ReviewFlag(flag=refuted, fingerprint=fingerprint(refuted), shown=False, verdict="refuted")]


def _write_flags(path: Path, items: list[ReviewFlag]) -> Path:
    path.write_text("".join(i.model_dump_json() + "\n" for i in items), encoding="utf-8")
    return path


def _export(tender: Path, tmp_path: Path, items: list[ReviewFlag] | None = None) -> Path:
    corpus = Corpus.load(tender / "prep")
    flags = _write_flags(tmp_path / "flags.jsonl", items or _items(corpus))
    code = main(
        ["export-review", COMPANY, "--fixture", str(FIXTURE / "clean"), "--flags", str(flags), "--export-id", EXPORT]
    )
    assert code == 0
    return tender / "labels" / f"review-{EXPORT}.xlsx"


def _key(tender: Path) -> dict:
    return json.loads((tender / "labels" / f"review-{EXPORT}.key.json").read_text())


def _fill(xlsx: Path, out: Path, decide) -> Path:
    wb = openpyxl.load_workbook(xlsx)
    ws = wb[SHEET]
    for r in range(2, ws.max_row + 1):
        decision, minutes = decide(ws.cell(r, 1).value)
        ws.cell(r, 4).value = decision
        ws.cell(r, 5).value = minutes
    wb.save(out)
    return out


# --- Export ---------------------------------------------------------------------------------------------------------


def test_fixture_data_dir_names_subfolders_apart(data_root: Path) -> None:
    a = paths.fixture_data_dir(COMPANY, FIXTURE / "clean", data_root)
    b = paths.fixture_data_dir(COMPANY, FIXTURE / "injected", data_root)
    assert a.name == "injection-tender-clean" and b.name == "injection-tender-injected"


def test_export_writes_to_the_data_folder_only(tender: Path, tmp_path: Path, data_root: Path) -> None:
    xlsx = _export(tender, tmp_path)
    assert xlsx.exists() and xlsx.is_relative_to(data_root)
    assert not xlsx.is_relative_to(paths.repo_root())


def test_export_is_blind(tender: Path, tmp_path: Path) -> None:
    xlsx = _export(tender, tmp_path)
    key = _key(tender)
    wb = openpyxl.load_workbook(xlsx)
    cells = [str(c.value) for ws in wb.worksheets for row in ws.iter_rows() for c in row if c.value is not None]
    text = "\n".join(cells).casefold()
    hidden = {r["lens_id"] for r in key["rows"]} | {r["claim_type"] for r in key["rows"]}
    hidden |= {r["fingerprint"][:12] for r in key["rows"]} | {"confirmed", "partly", "refuted", "kronor"}
    for word in hidden:
        assert word.casefold() not in text, word
    ws = wb[SHEET]
    assert tuple(c.value for c in ws[1]) == HEADER
    assert not any(d.hidden for d in ws.column_dimensions.values())
    assert not any(d.hidden for d in ws.row_dimensions.values())
    assert ws.protection.sheet
    formulas = [dv.formula1 for dv in ws.data_validations.dataValidation]
    assert any("Beslut" in f for f in formulas)  # the dropdown reads the taxonomy sheet
    assert [r[0].value for r in wb["Beslut"].iter_rows(min_row=2)] == [t.text for t in TAXONOMY]


def test_export_mixes_in_decoys(tender: Path, tmp_path: Path) -> None:
    _export(tender, tmp_path)
    rows = _key(tender)["rows"]
    kinds = [r["decoy"] for r in rows]
    assert kinds.count(None) == 3
    assert 1 <= len(rows) - 3 <= 2
    assert [r["item"] for r in rows] == [f"{n:03d}" for n in range(1, len(rows) + 1)]
    assert len({r["fingerprint"] for r in rows}) == len(rows)


def test_decoys_are_built_from_neighbours_and_sampled_flags(tender: Path) -> None:
    corpus = Corpus.load(tender / "prep")
    items = _items(corpus)
    shown = [i for i in items if i.shown]
    many = shown * 4  # 12 shown flags (repeats are fine for this): room for 3 decoys
    rows = build_review([*many, items[3]], corpus, EXPORT)
    decoys = [r for r in rows if r.decoy]
    assert len(decoys) == 3
    assert {"refuted"} <= {r.decoy for r in decoys}
    for row in decoys:
        if row.decoy == "neighbour_swap":
            source = next(i.flag for i in shown if fingerprint(i.flag) == row.source)
            (moved,) = [c for c in row.flag.citations if c not in source.citations]
            assert moved.quote in corpus.get(moved.block_id).text
            near = {b.id for c in source.citations for b in corpus.neighbours(c.block_id, n=2)}
            assert moved.block_id in near
            assert row.flag.claim == source.claim


def test_export_is_deterministic_for_an_export_id(tender: Path) -> None:
    corpus = Corpus.load(tender / "prep")
    a = build_review(_items(corpus), corpus, EXPORT)
    b = build_review(_items(corpus), corpus, EXPORT)
    assert [(r.item, r.fingerprint) for r in a] == [(r.item, r.fingerprint) for r in b]


def test_export_refuses_a_wrong_fingerprint_and_a_second_export(tender: Path, tmp_path: Path) -> None:
    corpus = Corpus.load(tender / "prep")
    bad = ReviewFlag(flag=_flag(corpus, "P1", "x"), fingerprint="0" * 64)
    with pytest.raises(LabelError, match="fingerprint"):
        read_review_flags(_write_flags(tmp_path / "bad.jsonl", [bad]))
    _export(tender, tmp_path)
    flags = tmp_path / "flags.jsonl"
    code = main(
        ["export-review", COMPANY, "--fixture", str(FIXTURE / "clean"), "--flags", str(flags), "--export-id", EXPORT]
    )
    assert code == 3


def test_export_refuses_fixtures_outside_synthetic(tmp_path: Path, data_root: Path) -> None:
    flags = _write_flags(tmp_path / "flags.jsonl", [])
    assert main(["export-review", COMPANY, "--fixture", str(tmp_path), "--flags", str(flags)]) == 3


# --- Import ---------------------------------------------------------------------------------------------------------


def test_import_writes_labels_and_decoys_apart(tender: Path, tmp_path: Path, capsys) -> None:
    xlsx = _export(tender, tmp_path)
    rows = {r["item"]: r for r in _key(tender)["rows"]}
    filled = _fill(xlsx, tmp_path / "filled.xlsx", lambda item: (WRONG if rows[item]["decoy"] else ACCEPT, 3))
    capsys.readouterr()
    assert (
        main(
            [
                "import-labels",
                COMPANY,
                "--fixture",
                str(FIXTURE / "clean"),
                "--xlsx",
                str(filled),
                "--labeller",
                "lab-1",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "Påstående" not in out and "»" not in out  # counts only
    labels = [json.loads(ln) for ln in (tender / "labels" / f"labels-{EXPORT}.jsonl").read_text().splitlines()]
    decoys = [json.loads(ln) for ln in (tender / "labels" / f"decoys-{EXPORT}.jsonl").read_text().splitlines()]
    real = {r["fingerprint"] for r in rows.values() if not r["decoy"]}
    assert {lab["fingerprint"] for lab in labels} == real
    assert all(lab["label"] == "accept" and lab["accepted"] and lab["blind"] for lab in labels)
    assert decoys and all(d["label"] == "wrong_quote" and not d["accepted"] and d["decoy"] for d in decoys)


def test_import_reports_decoys_separately_and_is_idempotent(tender: Path, tmp_path: Path) -> None:
    xlsx = _export(tender, tmp_path)
    rows = {r["item"]: r for r in _key(tender)["rows"]}
    first = sorted(rows)[0]
    filled = _fill(xlsx, tmp_path / "filled.xlsx", lambda item: (ACCEPT, None) if item != first else (None, None))
    for _ in range(2):
        summary = import_labels(COMPANY, tender, filled, "lab-1")
    decoys = sum(1 for r in rows.values() if r["decoy"])
    labelled_decoys = decoys - (1 if rows[first]["decoy"] else 0)
    assert summary["decoys"]["rows"] == decoys
    assert summary["decoys"]["labelled"] == labelled_decoys
    assert summary["decoys"]["accept_rate"] == (1.0 if labelled_decoys else None)
    assert summary["flags"]["labelled"] + labelled_decoys == len(rows) - 1
    text = (tender / "labels" / f"labels-{EXPORT}.jsonl").read_text()
    assert len(text.splitlines()) == summary["flags"]["labelled"]  # rewritten, not appended


def test_import_accepts_codes_and_comma_minutes(tender: Path, tmp_path: Path) -> None:
    xlsx = _export(tender, tmp_path)
    filled = _fill(xlsx, tmp_path / "filled.xlsx", lambda item: ("practice_differs", "2,5"))
    summary = import_labels(COMPANY, tender, filled, "lab-1")
    assert summary["minutes"] == 2.5 * len(_key(tender)["rows"])


def _refusal(tender: Path, path: Path, labeller: str = "lab-1") -> str:
    with pytest.raises(LabelError) as exc:
        import_labels(COMPANY, tender, path, labeller)
    return str(exc.value)


def test_import_refuses_bad_cells_naming_rows_only(tender: Path, tmp_path: Path) -> None:
    xlsx = _export(tender, tmp_path)
    secret = "Hemlig kommentar om kalkylen"
    filled = _fill(xlsx, tmp_path / "filled.xlsx", lambda item: (secret if item == "001" else ACCEPT, 900))
    message = _refusal(tender, filled)
    assert "the decision isn't one in the list" in message and "minutes must be" in message
    assert secret not in message and "row " in message
    assert not (tender / "labels" / f"labels-{EXPORT}.jsonl").exists()


def test_import_refuses_changed_structure(tender: Path, tmp_path: Path) -> None:
    xlsx = _export(tender, tmp_path)

    def edited(change) -> Path:
        wb = openpyxl.load_workbook(xlsx)
        change(wb)
        out = tmp_path / "edited.xlsx"
        wb.save(out)
        return out

    assert "missing" in _refusal(tender, edited(lambda wb: wb[SHEET].delete_rows(2)))
    assert "unknown number" in _refusal(tender, edited(lambda wb: wb[SHEET].cell(2, 1, "999")))
    assert "repeated" in _refusal(tender, edited(lambda wb: wb[SHEET].cell(3, 1, wb[SHEET].cell(2, 1).value)))
    assert "header" in _refusal(tender, edited(lambda wb: wb[SHEET].cell(1, 2, "Claim")))
    assert "no export" in _refusal(tender, edited(lambda wb: wb["Instruktion"].cell(1, 2, "ffffffff")))
    assert "labeller" in _refusal(tender, xlsx, labeller="Förnamn Efternamn")
    junk = tmp_path / "junk.xlsx"
    junk.write_bytes(b"not a workbook")
    assert "readable" in _refusal(tender, junk)


def test_workbook_round_trips_through_bytes(tender: Path, tmp_path: Path) -> None:
    xlsx = _export(tender, tmp_path)
    wb = openpyxl.load_workbook(io.BytesIO(xlsx.read_bytes()))
    assert wb.sheetnames == ["Instruktion", SHEET, "Beslut"]
