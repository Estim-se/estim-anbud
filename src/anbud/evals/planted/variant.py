"""One planted variant of a dev tender: three arms and an answer key, in the company's data folder.

    <tender>/planted/seed-<N>/
        arms/planted/prep/       the prep output with the document plants applied (a copy; the original is untouched)
        arms/planted/calc.xlsx   the calc with the calc plants, regenerated from typed posts
        arms/unplanted/prep/     the prep output, byte for byte: the false-positive baseline
        arms/unplanted/calc.xlsx the calc regenerated with no plant, in the same layout as the planted one
        arms/calc_only/prep/     a tender with no documents: control arm 2, the lenses on the calc alone
        arms/calc_only/calc.xlsx the planted calc
        key/plants.json          the answer key and control arm 1 (the structural detector); never read by a run

The same inputs and seed give the same files, byte for byte.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from random import Random

from anbud.evals.planted.calc import NO_SECTION, PostPlace, regenerate
from anbud.evals.planted.calc_plants import CalcEdit, apply, plant_calc
from anbud.evals.planted.categories import Category
from anbud.evals.planted.doc_plants import plant_documents
from anbud.evals.planted.key import EXPECTED_CLAIMS, Change, Expected, Key, Location, Plant, PostAt
from anbud.evals.planted.prep_copy import Prep, copy_prep
from anbud.evals.planted.structural import CatalogItem, detect
from anbud.granskning.contracts import CalcPost
from anbud.store import RunStore, StoreError, canonical

MAX_EVIDENCE_BLOCKS = 50
_BASIS = {"remove_category": "missing_post", "cut_quantity": "quantity_gap", "cut_hours": "hours_gap"}


def variant_dir(tender_dir: Path, seed: int) -> Path:
    return tender_dir / "planted" / f"seed-{seed}"


def _at(place: PostPlace, post: CalcPost) -> PostAt:
    return PostAt(sheet=place.sheet, row=place.row, section=place.section, text=post.text)


def _evidence(prep: Prep, category: Category) -> list[str]:
    """Blocks in the tender that name the category: what a flag about the plant would cite."""
    if category.key.startswith("section:"):
        return []
    hits = [b.id for b in prep.blocks if category.matches(b.text)]
    return hits[:MAX_EVIDENCE_BLOCKS]


def _calc_plant(
    n: int,
    edit: CalcEdit,
    posts: list[CalcPost],
    planted_posts: list[CalcPost],
    where: dict[int, int],
    places_planted: list[PostPlace],
    places_unplanted: list[PostPlace],
    prep_planted: Prep,
    prep_unplanted: Prep,
) -> Plant:
    changes = []
    for i in sorted(edit.touched()):
        old = posts[i]
        at = _at(places_planted[where[i]], planted_posts[where[i]]) if i in where else None
        new = edit.edited.get(i)
        action = edit.actions[i]
        before = after = None
        if action == "quantity_cut" and new is not None:
            before, after = old.quantity, new.quantity
        elif action == "hours_cut" and new is not None:
            before, after = old.hours, new.hours
        changes.append(
            Change(
                target="calc_post",
                action=action,  # type: ignore[arg-type]
                post_id=old.id,
                at=at,
                before=before,
                after=after,
                before_text=old.text,
                after_text=new.text if new is not None else None,
            )
        )
    sections = sorted({posts[i].section or NO_SECTION for i in edit.touched()})
    planted_at = [c.at for c in changes if c.at is not None and c.action != "mention_removed"]
    unplanted_at = [_at(places_unplanted[i], posts[i]) for i in sorted(edit.touched())]
    expected = Expected(
        claim_types=EXPECTED_CLAIMS[edit.kind],
        basis_kinds=[_BASIS[edit.kind]],  # type: ignore[list-item]
        quantity=edit.quantity(posts),
        hours=edit.hours(posts),
        kronor=edit.kronor(posts),
    )
    return Plant(
        id=f"p{n}",
        kind=edit.kind,
        family="calc",
        category=edit.category.key,
        changes=changes,
        expected=expected,
        locations={
            "planted": Location(posts=planted_at, sections=sections, blocks=_evidence(prep_planted, edit.category)),
            "unplanted": Location(
                posts=unplanted_at, sections=sections, blocks=_evidence(prep_unplanted, edit.category)
            ),
            "calc_only": Location(posts=planted_at, sections=sections),
        },
    )


def build_variant(
    company_id: str,
    tender_id: str,
    posts: list[CalcPost],
    prep_dir: Path,
    out_dir: Path,
    seed: int,
    calc_plants: int = 3,
    doc_plants: int = 2,
    catalog: dict[str, CatalogItem] | None = None,
    root: Path | None = None,
    force: bool = False,
) -> Key:
    store = RunStore(company_id, root)
    out = out_dir.resolve()
    if not out.is_relative_to(store.dir.resolve()) or out.parent.name != "planted" or not out.name.startswith("seed-"):
        raise StoreError("a planted variant goes in a tender's planted/seed-<N> folder in the company's folder")
    if out.exists():
        if not force:
            raise StoreError("this variant exists already; pass --force to write it again")
        shutil.rmtree(out)
    if not posts:
        raise StoreError("the calc has no posts to plant on")

    rng = Random(seed)
    prep_unplanted = Prep.load(prep_dir)
    prep_planted = Prep.load(prep_dir)

    edits = plant_calc(posts, rng, calc_plants)
    planted_posts, where = apply(posts, edits)
    unplanted_xlsx, places_unplanted = regenerate(posts)
    planted_xlsx, places_planted = regenerate(planted_posts)
    doc = plant_documents(prep_planted, rng, doc_plants)

    plants = [
        _calc_plant(
            n,
            edit,
            posts,
            planted_posts,
            where,
            places_planted,
            places_unplanted,
            prep_planted,
            prep_unplanted,
        )
        for n, edit in enumerate(edits, 1)
    ]
    for d in doc:
        plants.append(
            Plant(
                id=f"p{len(plants) + 1}",
                kind=d.kind,
                family="document",
                changes=d.changes,
                expected=d.expected,
                locations={"planted": d.planted, "unplanted": d.unplanted, "calc_only": Location()},
            )
        )

    key = Key(
        company_id=company_id,
        tender_id=tender_id,
        seed=seed,
        source={
            "calc_posts": hashlib.sha256(canonical([p.model_dump() for p in posts])).hexdigest(),
            "blocks": hashlib.sha256((prep_dir / "blocks.jsonl").read_bytes()).hexdigest(),
        },
        plants=plants,
        structural=detect(planted_xlsx, catalog),
        structural_unplanted=detect(unplanted_xlsx, catalog),
    )

    arms = out / "arms"
    prep_planted.write(store, arms / "planted" / "prep")
    store.write_file(arms / "planted" / "calc.xlsx", planted_xlsx)
    copy_prep(store, prep_dir, arms / "unplanted" / "prep")
    store.write_file(arms / "unplanted" / "calc.xlsx", unplanted_xlsx)
    Prep.empty().write(store, arms / "calc_only" / "prep")
    store.write_file(arms / "calc_only" / "calc.xlsx", planted_xlsx)
    store.write_file(out / "key" / "plants.json", key.model_dump_json(indent=2).encode())
    return key
