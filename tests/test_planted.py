"""Planted defects: calc regeneration, natural plants, document patches, both control arms and the matcher.

Everything runs on the synthetic tender in `planted_synth.py`, in temp folders. No model call, no real data.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from random import Random

import openpyxl
import pytest
from planted_synth import calc_posts, write_prep, write_tender

from anbud import cli, paths
from anbud.evals import split_heldout as sh
from anbud.evals.planted.calc import read_rows, regenerate
from anbud.evals.planted.calc_plants import _strip_mention, apply, plant_calc
from anbud.evals.planted.categories import CATEGORIES
from anbud.evals.planted.doc_plants import clause, contradicting_quantity, delete_section, drawing_note_conflict
from anbud.evals.planted.key import Key, Plant
from anbud.evals.planted.prep_copy import Prep
from anbud.evals.planted.score import RunFlags, match_arm, score
from anbud.evals.planted.structural import CatalogItem, detect, item_key
from anbud.evals.planted.variant import build_variant, variant_dir
from anbud.granskning.contracts import CalcPost, RunInputs, fingerprint
from anbud.granskning.corpus import Corpus
from anbud.granskning.items import Item, Kronor
from anbud.models import Citation, Flag, MoneyBasis
from anbud.prep.text import TEXT_PREFIX, text_hash

COMPANY, TENDER = "co-001", "t-0001"


def _tender(data_root: Path) -> Path:
    tender = paths.dev_tender_dir(COMPANY, TENDER)
    write_tender(tender)
    return tender


def _variant(data_root: Path, seed: int = 1, **kw: int) -> tuple[Path, Key]:
    tender = _tender(data_root)
    out = variant_dir(tender, seed)
    key = build_variant(COMPANY, TENDER, calc_posts(), tender / "prep", out, seed, **kw)
    return out, key


def _ingest(xlsx: bytes) -> list[CalcPost]:
    """Stands in for the calc ingest: one post per priced or note row, with its section heading."""
    posts, section = [], None
    for row in read_rows(xlsx):
        if row.kind == "heading":
            section = row.text
        elif row.kind in ("post", "note"):
            posts.append(
                CalcPost(
                    id=f"c-{row.sheet}-{row.row}",
                    sheet=row.sheet,
                    row=row.row,
                    section=section,
                    text=row.text,
                    quantity=row.quantity,
                    unit=row.unit,
                    unit_price=row.unit_price,
                    amount=row.amount,
                    hours=row.hours,
                )
            )
    return posts


# Regeneration


def test_regenerated_calc_is_consistent_and_byte_stable() -> None:
    posts = calc_posts()
    data, places = regenerate(posts)
    assert regenerate(posts)[0] == data
    assert detect(data) == []
    rows = read_rows(data)
    numbers = [r.number for r in rows if r.kind == "post"]
    assert numbers[:3] == ["1.1", "1.2", "2.1"]
    total = next(r for r in rows if r.kind == "total")
    assert total.amount == pytest.approx(sum((p.quantity or 0) * (p.unit_price or 0) or (p.amount or 0) for p in posts))
    assert len(places) == len(posts)
    assert places[-1].section == "Övrigt"  # a post with no section closes the sheet


def test_editing_the_xlsx_directly_gives_the_plant_away() -> None:
    """Why plants go through the posts: a deleted row leaves a numbering gap and a stale subtotal."""
    data, places = regenerate(calc_posts())
    wb = openpyxl.load_workbook(io.BytesIO(data))
    wb["Kalkyl"].delete_rows(places[4].row)  # the dimmer post
    buf = io.BytesIO()
    wb.save(buf)
    checks = {f.check for f in detect(buf.getvalue())}
    assert {"post_numbering", "subtotal"} <= checks


def test_planting_through_posts_leaves_no_structural_tell() -> None:
    posts = calc_posts()
    for seed in range(25):
        planted, _ = apply(posts, plant_calc(posts, Random(seed), 3))
        assert detect(regenerate(planted)[0]) == [], seed


def test_detector_catalog_and_own_norm_diffs() -> None:
    posts = calc_posts()
    data, places = regenerate(posts)
    catalog = {item_key("Dimmer", "st"): CatalogItem(unit_price=450, hours_per_unit=1.0)}
    hits = detect(data, catalog)
    assert [(f.check, f.row) for f in hits] == [("catalog_hours", places[4].row)]
    twice = [*posts, posts[4].model_copy(update={"id": "x", "section": "Projektledning", "hours": 30})]
    assert {f.check for f in detect(regenerate(twice)[0])} == {"hours_norm"}


# Calc plants


def test_category_plants_are_whole_categories_on_disjoint_posts() -> None:
    posts = calc_posts()
    for seed in range(25):
        edits = plant_calc(posts, Random(seed), 3)
        assert len(edits) == 3
        assert len({e.kind for e in edits}) == 3
        touched = [e.touched() for e in edits]
        assert sum(map(len, touched)) == len(set().union(*touched))
        for edit in edits:
            if edit.kind == "remove_category":
                assert len(edit.removed) >= 2  # never a single row on its own
                category = edit.category
                left = [p for i, p in enumerate(posts) if i not in edit.removed]
                assert not any(category.matches(p.text) for p in left if p not in edit.edited.values())
            for i, new in edit.edited.items():
                if edit.kind == "cut_quantity":
                    assert new.quantity < posts[i].quantity
                    if posts[i].hours:  # hours follow, so the hours per unit stay at the calc's norm
                        ratio = (new.hours / new.quantity) / (posts[i].hours / posts[i].quantity)
                        assert ratio == pytest.approx(1, abs=0.01)
                if edit.kind == "cut_hours":
                    assert 0 < new.hours < posts[i].hours


def test_same_seed_same_plants_and_a_mention_loses_only_its_clause() -> None:
    posts = calc_posts()
    a = [(e.kind, e.category.key, sorted(e.touched())) for e in plant_calc(posts, Random(4), 3)]
    b = [(e.kind, e.category.key, sorted(e.touched())) for e in plant_calc(posts, Random(4), 3)]
    assert a == b
    switches = next(c for c in CATEGORIES if c.key == "switches")
    assert _strip_mention("Elcentral, inkl. strömbrytare i trapphus", switches) == "Elcentral"
    assert _strip_mention("Strömbrytare infälld", switches) is None


# Document plants


def _prep(tmp_path: Path) -> Prep:
    write_prep(tmp_path / "prep")
    return Prep.load(tmp_path / "prep")


def test_contradicting_quantity_changes_one_of_a_pair(tmp_path: Path) -> None:
    prep = _prep(tmp_path)
    plant = contradicting_quantity(prep, Random(0), set())
    assert plant is not None
    change = plant.changes[0]
    assert change.action == "replaced" and change.after > change.before
    new = next(b for b in prep.blocks if b.id == change.new_block_id)
    assert new.id.split("-")[1] == text_hash(new.text)[:TEXT_PREFIX]  # an id the way prep makes one
    assert change.block_id not in {b.id for b in prep.blocks}
    assert set(plant.planted.blocks) == {change.new_block_id, *plant.unplanted.blocks} - {change.block_id}


def test_clauses_go_in_the_contract_document(tmp_path: Path) -> None:
    prep = _prep(tmp_path)
    af = prep.blocks[0].doc_id
    vite = clause("vite_clause", prep, Random(1), set())
    assert vite is not None and vite.expected.formula == "vite_per_week"
    block = next(b for b in prep.blocks if b.id == vite.changes[0].new_block_id)
    assert block.doc_id == af and "vite" in block.text
    assert vite.unplanted.anywhere
    assert clause("vite_clause", prep, Random(1), set()) is None  # never a second one


def test_delete_section_leaves_a_dangling_reference(tmp_path: Path) -> None:
    prep = _prep(tmp_path)
    plant = delete_section(prep, Random(0), set())
    assert plant is not None
    deleted = {c.block_id for c in plant.changes}
    assert len(deleted) == 3  # the AFC.4 heading and its two paragraphs
    assert not deleted & {b.id for b in prep.blocks}
    assert not deleted & {o.block_id for o in prep.outline}
    referring = [b for b in prep.blocks if b.id in plant.planted.blocks]
    assert referring and all("AFC.4" in b.text for b in referring)


def test_drawing_note_goes_on_the_drawing(tmp_path: Path) -> None:
    prep = _prep(tmp_path)
    drawing = prep.blocks[-1].doc_id
    plant = drawing_note_conflict(prep, Random(2), set())
    assert plant is not None
    note = next(b for b in prep.blocks if b.id == plant.changes[0].new_block_id)
    assert note.doc_id == drawing and note.kind == "tile_text"


# The variant


def _digest(folder: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            h.update(path.name.encode() + path.read_bytes())
    return h.hexdigest()


def test_variant_writes_three_arms_and_keeps_the_original(data_root: Path) -> None:
    tender = _tender(data_root)
    before = _digest(tender / "prep")
    out = variant_dir(tender, 3)
    key = build_variant(COMPANY, TENDER, calc_posts(), tender / "prep", out, 3)
    assert _digest(tender / "prep") == before
    arms = out / "arms"
    assert _digest(arms / "unplanted" / "prep") == before
    planted = Corpus.load(arms / "planted" / "prep")
    assert len(planted.blocks) == len(planted.order)  # ids stay unique
    assert Corpus.load(arms / "calc_only" / "prep").blocks == {}
    assert (arms / "calc_only" / "calc.xlsx").read_bytes() == (arms / "planted" / "calc.xlsx").read_bytes()
    assert not list(arms.rglob("plants.json"))  # the key sits outside every arm
    assert Key.model_validate_json((out / "key" / "plants.json").read_text()) == key
    docs = json.loads((arms / "planted" / "prep" / "documents.json").read_text())
    assert sum(d["blocks"] for d in docs) == len(planted.blocks)
    assert {p.family for p in key.plants} == {"calc", "document"}
    assert key.structural == [] and key.structural_unplanted == []


def test_same_seed_same_bytes_and_force_needed_to_rewrite(data_root: Path) -> None:
    out, _ = _variant(data_root, seed=5)
    first = _digest(out)
    with pytest.raises(Exception, match="exists"):
        build_variant(COMPANY, TENDER, calc_posts(), out.parents[1] / "prep", out, 5)
    build_variant(COMPANY, TENDER, calc_posts(), out.parents[1] / "prep", out, 5, force=True)
    assert _digest(out) == first


def test_plant_locations_point_at_the_planted_calc(data_root: Path) -> None:
    out, key = _variant(data_root, seed=2)
    rows = {(r.sheet, r.row): r for r in read_rows((out / "arms" / "planted" / "calc.xlsx").read_bytes())}
    for plant in key.plants:
        for change in plant.changes:
            if change.at is not None:
                row = rows[(change.at.sheet, change.at.row)]
                assert row.text == change.at.text
                if change.action == "quantity_cut":
                    assert row.quantity == change.after


# The matcher


def _flag(claim_type: str, kind: str, blocks: list[str] | None = None, **basis: object) -> Flag:
    return Flag(
        lens_id="lens",
        claim_type=claim_type,
        claim="Synthetic claim.",
        citations=[Citation(block_id=b, quote="q") for b in (blocks or ["none-000000"])],
        money_basis=MoneyBasis(kind=kind, **basis),  # type: ignore[arg-type]
    )


def _perfect(plant: Plant, posts: dict[tuple[str, int], str]) -> Flag:
    """The flag a perfect run would raise for the plant, on the planted arm."""
    loc = plant.locations["planted"]
    exp = plant.expected
    kind = exp.basis_kinds[0]
    basis: dict[str, object] = {"formula": exp.formula}
    if loc.posts:
        basis["post_ref"] = posts[(loc.posts[0].sheet, loc.posts[0].row)]
    if kind == "quantity_gap" and plant.family == "calc":
        change = next(c for c in plant.changes if c.at == loc.posts[0])
        basis["quantity"] = change.before - change.after
    elif kind == "quantity_gap":
        basis["quantity"] = exp.quantity
    if kind == "hours_gap":
        change = next(c for c in plant.changes if c.at == loc.posts[0])
        basis["hours"] = change.before - change.after
    if kind == "missing_post":
        basis["item"] = plant.changes[0].before_text
    return _flag(exp.claim_types[0], kind, loc.blocks[:1] or None, **basis)


def _runs(out: Path) -> dict[str, list[CalcPost]]:
    return {arm: _ingest((out / "arms" / arm / "calc.xlsx").read_bytes()) for arm in ("planted", "unplanted")}


def test_a_perfect_run_catches_every_plant_by_rule(data_root: Path) -> None:
    out, key = _variant(data_root, seed=2)
    posts = _runs(out)["planted"]
    ids = {(p.sheet, p.row): p.id for p in posts}
    flags = [_perfect(p, ids) for p in key.plants]
    matches = match_arm(key, "planted", flags, posts)
    assert [(m.plant_id, m.outcome) for m in matches] == [(p.id, "rule") for p in key.plants]
    report = score(key, {"planted": RunFlags(run_id="r1", flags=flags, calc_posts=posts)})
    assert report.recall_clean["all"].recall == 1.0
    assert report.complete is False  # no calc-only run yet


def test_partial_matches_go_to_the_judge_and_wrong_direction_never_counts(data_root: Path) -> None:
    out, key = _variant(data_root, seed=2)
    posts = _runs(out)["planted"]
    ids = {(p.sheet, p.row): p.id for p in posts}
    cut = next(p for p in key.plants if p.kind == "cut_quantity")
    good = _perfect(cut, ids)
    wrong_type = good.model_copy(update={"claim_type": "something_else"})
    over = good.model_copy(update={"money_basis": good.money_basis.model_copy(update={"quantity": -5.0})})
    far = good.model_copy(update={"money_basis": good.money_basis.model_copy(update={"quantity": 10_000.0})})
    elsewhere = _flag(cut.expected.claim_types[0], "quantity_gap", post_ref="no-such-post", quantity=1.0)
    matches = {m.flag: m for m in match_arm(key, "planted", [wrong_type, over, far, elsewhere], posts)}
    assert matches[0].outcome == "unresolved"  # the judge stub decides nothing
    assert matches[1].size is False and matches[1].outcome == "unresolved"
    assert matches[2].size is False and matches[2].outcome == "unresolved"
    assert 3 not in matches  # points nowhere the plant is


def test_control_arms_and_baseline_in_the_report(data_root: Path) -> None:
    out, key = _variant(data_root, seed=2)
    runs = _runs(out)
    ids = {(p.sheet, p.row): p.id for p in runs["planted"]}
    flags = [_perfect(p, ids) for p in key.plants]
    calc_plant = next(p for p in key.plants if p.family == "calc" and p.kind != "remove_category")
    calc_only_flag = _perfect(calc_plant, ids)
    # The unplanted arm flags the cut post at its unplanted place: the baseline counts it.
    u_ids = {(p.sheet, p.row): p.id for p in runs["unplanted"]}
    u_loc = calc_plant.locations["unplanted"].posts[0]
    u_flag = calc_only_flag.model_copy(
        update={
            "money_basis": calc_only_flag.money_basis.model_copy(update={"post_ref": u_ids[(u_loc.sheet, u_loc.row)]})
        }
    )
    report = score(
        key,
        {
            "planted": RunFlags(run_id="p", flags=flags, calc_posts=runs["planted"]),
            "calc_only": RunFlags(run_id="c", flags=[calc_only_flag], calc_posts=runs["planted"]),
            "unplanted": RunFlags(run_id="u", flags=[u_flag], calc_posts=runs["unplanted"]),
        },
    )
    assert report.complete
    n = len(key.plants)
    assert report.recall_clean["all"].plants == n - 1  # the plant the calc-only arm caught is left out
    assert report.recall_all["all"].plants == n
    assert report.controls["calc_only_share"] == round(1 / n, 4)
    assert report.controls["calc_only_too_easy"] is False
    assert report.baseline == {"flags": 1, "flags_matching_a_plant": 1, "plants_hit": 1}
    assert report.flags["planted"]["unmatched"] == 0


# CLI


def _dev(data_root: Path) -> None:
    sh.append_events(paths.ledger_path(COMPANY), [sh._assign(TENDER, sh.DEV, "test", ())])


def test_cli_plant_and_score(data_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _dev(data_root)
    tender = _tender(data_root)
    assert cli.main(["plant", COMPANY, TENDER, "--seed", "2"]) == 0
    out = variant_dir(tender, 2)
    assert (out / "key" / "plants.json").exists()
    assert cli.main(["plant", COMPANY, TENDER, "--seed", "2"]) == 3  # exists: needs --force
    key = Key.model_validate_json((out / "key" / "plants.json").read_text())
    posts = _ingest((out / "arms" / "planted" / "calc.xlsx").read_bytes())
    ids = {(p.sheet, p.row): p.id for p in posts}
    # A run's output as `anbud run` writes it, under the tender's runs/.
    run = tender / "runs" / "run-1"
    run.mkdir(parents=True)
    inputs = RunInputs(
        company_id=COMPANY,
        tender_id=TENDER,
        prep_dir=str(out / "arms" / "planted" / "prep"),
        calc_posts=posts,
        arm="planted/seed-2/arms/planted",
    )
    (run / "inputs.json").write_text(inputs.model_dump_json())
    flags = [_perfect(p, ids) for p in key.plants]
    fps = [fingerprint(f) for f in flags]
    (run / "flags.json").write_text(json.dumps([f.model_dump(mode="json") for f in flags]))
    kronor = {
        fp: Kronor(priced=True, point=p.expected.kronor or 1000.0, basis=f.money_basis, label="syntetisk").model_dump(
            mode="json"
        )
        for f, fp, p in zip(flags, fps, key.plants, strict=True)
    }
    (run / "kronor.json").write_text(json.dumps(kronor))
    placements = ["refuted", *["shown"] * (len(flags) - 1)]
    items = [
        Item(flag=f, fingerprint=fp, lenses=["lens"], placement=pl).model_dump(mode="json")
        for f, fp, pl in zip(flags, fps, placements, strict=True)
    ]
    (run / "items.json").write_text(json.dumps(items))
    capsys.readouterr()
    assert cli.main(["plant-score", "run-1"]) == 0
    printed = capsys.readouterr().out
    assert f"recall (both controls miss) all: {len(key.plants)}/{len(key.plants)}" in printed
    assert "Strömbrytare" not in printed and "AFC" not in printed  # counts and ids only
    report = json.loads((out / "key" / "scores" / "run-1.json").read_text())
    assert report["recall_clean"]["all"]["recall"] == 1.0
    assert report["recall_clean"]["all"]["seen"] == len(key.plants) - 1  # the refuted one isn't shown


def test_cli_plant_refuses_held_out_and_unprepared(data_root: Path) -> None:
    sh.append_events(paths.ledger_path(COMPANY), [sh._assign("t-0002", sh.HELDOUT, "test", ())])
    assert cli.main(["plant", COMPANY, "t-0002", "--seed", "1"]) == 3
    _dev(data_root)
    assert cli.main(["plant", COMPANY, TENDER, "--seed", "1"]) == 3  # not prepared
    assert cli.main(["plant-score", "no-such-run"]) == 3
