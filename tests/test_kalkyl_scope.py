"""The scope pass, room composition, recipes and multiplying, on invented tenders. No model call is real, $0.

Every text, id, count and price here is invented; the recipe figures are test values, not anyone's standard.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from kalkyl_synthetic import (
    CATALOG,
    cite,
    fake_sdk,
    first_block,
    good_handler,
    message,
    obj,
    req,
    submit,
    write_catalog,
    write_prep,
)

from anbud import paths
from anbud.granskning.catalog import Catalog
from anbud.granskning.corpus import Corpus
from anbud.kalkyl.export_recipe import build, parse_recipe_source
from anbud.kalkyl.extract import BRIEF, chunk_message
from anbud.kalkyl.lines import heading_paths, make_chunks, scope_line_ids
from anbud.kalkyl.pipeline import with_room_descriptors
from anbud.kalkyl.price import DerivedQuantity, build_posts, count_objects, price_posts
from anbud.kalkyl.recipe import CUSTOMER_LABEL, DEFAULT_LABEL, Recipe, RecipeError, RecipeItem, bind, load_recipe
from anbud.kalkyl.records import Contained, QuantityRecord, VerifiedRecord
from anbud.kalkyl.rooms import descriptor, room_types
from anbud.kalkyl.run import run_dev_tender
from anbud.kalkyl.scope_lines import is_scope_line, object_kinds_in
from anbud.kalkyl.verify import check_record
from anbud.models import Block, Citation

SCOPE_CATALOG = {
    **CATALOG,
    "items": [
        *CATALOG["items"],
        {"id": "takuttag", "name": "Takuttag", "unit": "st", "unit_price": 40, "hours": 0.3, "category": "Uttag"},
        {"id": "bankuttag", "name": "Bänkuttag", "unit": "st", "unit_price": 95, "hours": 0.5, "category": "Kök"},
    ],
}
TEST_RECIPE = Recipe(
    name="test-recipe",
    label=DEFAULT_LABEL,
    source="invented test values",
    per_apartment={"hall": 1},
    items=[
        RecipeItem(room="bedroom", item="Vägguttag dubbelt", catalog_id="uttag-dubbelt", count=3),
        RecipeItem(room="living_room", item="Vägguttag dubbelt", catalog_id="uttag-dubbelt", count=5),
        RecipeItem(room="living_room", item="Takuttag", count=2),  # matched by name
        RecipeItem(room="hall", item="Takuttag", catalog_id="takuttag", count=1),
        RecipeItem(room="kitchen", item="Bänkuttag", catalog_id="bankuttag", count=4),
        RecipeItem(room="half_room", item="Saknas i katalogen", count=9),  # unbound: never used
    ],
)


def _catalog() -> Catalog:
    return Catalog.model_validate(SCOPE_CATALOG)


def _verified(records: list[dict]) -> list[VerifiedRecord]:
    out = []
    for n, raw in enumerate(records, 1):
        r = QuantityRecord.model_validate(raw)
        ids = [c.block_id for c in r.citations]
        out.append(VerifiedRecord(id=f"c001-r{n:03d}", chunk_id="c001", record=r, level="exact", block_ids=ids))
    return out


def _row(n: int, rooms: float, kitchen: bool = True) -> dict:
    held = [{"object_kind": "room", "quantity": rooms}] + ([{"object_kind": "kitchen", "quantity": 1}] * kitchen)
    return obj("apartment", "list_row", [cite(f"list-{n:03d}", f"{n}")], contains=held)


# --- scope lines -------------------------------------------------------------------------------------------------


def _p(text: str, kind: str = "paragraph") -> Block:
    return Block(id="x", doc_id="d", kind=kind, text=text)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "heading", "expected"),
    [
        ("Sovrum förses med dubbla vägguttag och takuttag.", "", True),
        ("Varje lägenhet utrustas komplett enligt standard.", "", True),  # a verb and an object kind
        ("Uttag placeras vid varje säng.", "Sovrum", True),  # the room type is in the heading
        ("Badrum: spotlights i tak och uttag vid spegel.", "", True),
        ("Kabelstegar i källare.", "", False),  # no item verb or room
        ("Lägenheterna har parkettgolv.", "", False),  # an object, but nothing installed
        ("Uttag enligt el-ritning.", "Allmänt", False),  # an item, but no object kind or room type
        ("Entreprenaden omfattar elarbeten.", "", False),
    ],
)
def test_scope_lines_are_found_by_form_and_heading(text: str, heading: str, expected: bool) -> None:
    assert is_scope_line(_p(text), heading, quantity=False) is expected


def test_a_quantity_line_or_heading_is_never_a_scope_line() -> None:
    assert not is_scope_line(_p("Sovrum: 2 st uttag."), "", quantity=True)
    assert not is_scope_line(_p("Uttag i sovrum", kind="heading"), "", quantity=False)
    assert not is_scope_line(_p("Sovrum | dubbla uttag", kind="sheet_row"), "", quantity=False)


def test_object_words_map_to_the_enum() -> None:
    assert object_kinds_in("Lägenheternas sovrum och hallen") == ["apartment", "bedroom", "hall"]
    assert object_kinds_in("Sovrummen") == ["bedroom"]
    assert object_kinds_in("rumstemperatur") == []  # a compound isn't a room


def test_the_synthetic_tender_has_one_scope_line_and_the_message_marks_it(tmp_path: Path) -> None:
    corpus = Corpus.load(write_prep(tmp_path / "prep"))
    assert scope_line_ids(corpus) == {"aaaaaa-000008"}
    chunk = make_chunks(corpus, 12000, 30)[0]
    assert chunk.scope_ids == ["aaaaaa-000008"]
    msg = chunk_message(chunk, corpus, heading_paths(corpus))
    assert 'id="aaaaaa-000008" kind="paragraph" page="2" s="1"' in msg
    assert "1 scope line(s)" in msg
    assert 's="1"' in BRIEF and "complete_installation" in BRIEF


# --- citations for per-object requirements -----------------------------------------------------------------------


def _check(raw: dict, tmp_path: Path) -> str | None:
    corpus = Corpus.load(write_prep(tmp_path / "prep"))
    return check_record(QuantityRecord.model_validate(raw), make_chunks(corpus, 12000, 30)[0], corpus)


def test_a_per_object_requirement_may_leave_its_count_out(tmp_path: Path) -> None:
    raw = req("Central", "per_object", "st", [cite("aaaaaa-000008", "lägenhetscentral")], per_object="apartment")
    assert _check(raw, tmp_path) is None


def test_a_complete_installation_carries_no_count(tmp_path: Path) -> None:
    base = req("Allt", "per_object", "st", [cite("aaaaaa-000008", "lägenhet")], per_object="apartment")
    assert _check({**base, "complete_installation": True}, tmp_path) is None
    assert "leaves quantity out" in (_check({**base, "complete_installation": True, "quantity": 1}, tmp_path) or "")
    stated = req("Allt", "stated", "st", [cite("aaaaaa-000005", "24 st")], quantity=24, complete_installation=True)
    assert "per_object requirements" in (_check(stated, tmp_path) or "")


def test_a_number_word_counts_as_written(tmp_path: Path) -> None:
    blocks = [Block(id="aaaaaa-000001", doc_id="a" * 12, kind="paragraph", text="Två uttag i varje sovrum.")]
    corpus = Corpus.load(write_prep(tmp_path / "prep", blocks, []))
    chunk = make_chunks(corpus, 12000, 30)[0]
    ok = req("Uttag", "per_object", "st", [cite("aaaaaa-000001", "Två uttag")], quantity=2, per_object="bedroom")
    assert check_record(QuantityRecord.model_validate(ok), chunk, corpus) is None
    bad = {**ok, "quantity": 3}
    assert "isn't written" in (check_record(QuantityRecord.model_validate(bad), chunk, corpus) or "")


# --- room composition --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("A-1101 | 3 rok | 71,0", [("room", 3.0), ("kitchen", 1.0)]),
        ("A-1102 | 2 RoK", [("room", 2.0), ("kitchen", 1.0)]),
        ("A-1103 | 4 rum och kök", [("room", 4.0), ("kitchen", 1.0)]),
        ("A-1104 | 1 r.o.k.", [("room", 1.0), ("kitchen", 1.0)]),
        ("A-1105 | 3,5 rok", [("room", 3.5), ("kitchen", 1.0)]),
        ("A-1106 | 2 rum", [("room", 2.0)]),
        ("A-1107 | trea", [("room", 3.0)]),
        ("A-1108 | 64,5 rum", []),  # an area next to the word, not a room count
        ("A-1109 | 2 rok | 3 rok", []),  # two different descriptors: left alone
        ("1001 | 3 | 1", []),
    ],
)
def test_room_descriptors_are_read_from_the_row(text: str, expected: list[tuple[str, float]]) -> None:
    assert [(c.object_kind, c.quantity) for c in descriptor(text)] == expected


def test_rooms_give_room_types_and_a_stated_bedroom_count_wins() -> None:
    three = room_types([Contained(object_kind="room", quantity=3), Contained(object_kind="kitchen", quantity=1)])
    assert three == {"room": 3, "kitchen": 1, "living_room": 1, "bedroom": 2}
    half = room_types([Contained(object_kind="room", quantity=2.5)])
    assert half == {"room": Decimal("2.5"), "living_room": 1, "bedroom": 1, "half_room": 1}
    stated = room_types([Contained(object_kind="room", quantity=4), Contained(object_kind="bedroom", quantity=2)])
    assert stated["bedroom"] == 2
    assert room_types([Contained(object_kind="room", quantity=12)]) == {}  # eleven bedrooms: a wrong column


def test_code_fills_contains_from_the_cited_row_only_for_apartment_list_rows(tmp_path: Path) -> None:
    blocks = [
        Block(id="aaaaaa-000001", doc_id="a" * 12, kind="table_row", text="A-1101 | 3 rok | 71,0"),
        Block(id="aaaaaa-000002", doc_id="a" * 12, kind="table_row", text="A-1102 | 2 rum och kök | 55,0"),
        Block(id="aaaaaa-000003", doc_id="a" * 12, kind="table_row", text="Lokal | 2 rum | 40,0"),
        Block(id="aaaaaa-000004", doc_id="a" * 12, kind="paragraph", text="Totalt 2 lägenheter om 3 rok."),
    ]
    corpus = Corpus.load(write_prep(tmp_path / "prep", blocks, []))
    verified = _verified(
        [
            obj("apartment", "list_row", [cite("aaaaaa-000001", "A-1101")]),
            obj("apartment", "list_row", [cite("aaaaaa-000002", "A-1102")]),
            obj("premises", "list_row", [cite("aaaaaa-000003", "Lokal")]),
            obj("apartment", "stated", [cite("aaaaaa-000004", "2 lägenheter")], quantity=2),
        ]
    )
    filled, n = with_room_descriptors(verified, corpus)
    assert n == 2
    assert [c.object_kind for c in filled[0].record.contains] == ["room", "kitchen"]
    assert not filled[2].record.contains and not filled[3].record.contains  # premises and a stated total: left alone
    objects = count_objects(filled)
    assert {k: objects[k].count for k in ("apartment", "room", "living_room", "bedroom", "kitchen")} == {
        "apartment": 2,
        "room": 5,
        "living_room": 2,
        "bedroom": 3,
        "kitchen": 2,
    }
    assert "per lägenhetstyp: 2 rum och kök: 1 lgh × 1; 3 rum och kök: 1 lgh × 2" in objects["bedroom"].basis
    assert objects["bedroom"].rows == ["aaaaaa-000001", "aaaaaa-000002"]


def test_room_types_from_fewer_rows_than_apartments_say_so() -> None:
    rows = [_row(1, 3), _row(2, 2), obj("apartment", "list_row", [cite("list-003", "3")])]
    objects = count_objects(_verified(rows))
    assert objects["apartment"].count == 3 and objects["bedroom"].count == 3
    assert "rumstyper från 2 lägenhetsrader med rumsuppgift av 3" in (objects["bedroom"].note or "")


def test_a_hall_per_apartment_comes_from_the_recipe_labelled() -> None:
    recipe = bind(TEST_RECIPE, _catalog())
    objects = count_objects(_verified([_row(1, 3), _row(2, 2)]), recipe)
    assert objects["hall"].count == 2
    assert DEFAULT_LABEL in objects["hall"].basis and DEFAULT_LABEL in (objects["hall"].note or "")
    assert "hall" not in count_objects(_verified([_row(1, 3)]))  # no recipe: no assumed halls


def test_a_bathroom_per_apartment_from_the_recipe_counts_per_bathroom_requirements() -> None:
    recipe = TEST_RECIPE.model_copy(update={"per_apartment": {"hall": 1, "bathroom": 1}})
    record = req(
        "Handdukstork", "per_object", "st", SCOPE_CITE, quantity=1, per_object="bathroom", catalog_id="uttag-dubbelt"
    )
    (post,) = [p for p in _posts([record], recipe) if p.lines[0].record.record.what == "Handdukstork"]
    assert post.quantity == 3 and post.status == "Prissatt"  # 1 per bathroom × 1 bathroom in each of 3 apartments
    assert post.lines[0].objects is not None and DEFAULT_LABEL in post.lines[0].objects.basis


# --- multiplying -------------------------------------------------------------------------------------------------

SCOPE_CITE = [cite("fu-001", "Sovrum förses med uttag")]


def _posts(records: list[dict], recipe=None, derived=None):
    catalog = _catalog()
    verified = _verified([_row(1, 3), _row(2, 2), _row(3, 1), *records])
    bound = bind(recipe, catalog)
    objects = count_objects(verified, bound)
    posts = build_posts(verified, objects, catalog, bound, derived)
    price_posts(posts, catalog)
    return posts


def test_a_stated_count_per_room_type_multiplies_by_that_room_count() -> None:
    record = req(
        "Dubbla uttag i sovrum",
        "per_object",
        "st",
        SCOPE_CITE,
        quantity=2,
        per_object="bedroom",
        catalog_id="uttag-dubbelt",
    )
    (post,) = [p for p in _posts([record]) if p.lines[0].record.record.what.startswith("Dubbla")]
    assert post.quantity == 6 and post.status == "Prissatt"  # 2 per bedroom × (2 + 1 + 0) bedrooms
    assert post.basis == "2 per sovrum × 3 sovrum"
    assert post.rows == ["list-001", "list-002"]  # the rows with bedrooms, cited
    assert post.level == "Exakt citat"


def test_without_a_count_the_recipe_gives_it_labelled() -> None:
    record = req("Uttag i sovrum", "per_object", "st", SCOPE_CITE, per_object="bedroom", catalog_id="uttag-dubbelt")
    (post,) = _posts([record], TEST_RECIPE)
    assert post.quantity == 9 and post.status == "Prissatt"
    assert post.basis == f"3 per sovrum ({DEFAULT_LABEL}) × 3 sovrum"
    assert f"Antal per objekt enligt {DEFAULT_LABEL}" in post.notes
    assert post.level == "Enligt recept"


def test_without_a_count_or_recipe_the_post_is_missing_a_quantity_with_its_multiplier() -> None:
    record = req("Uttag i sovrum", "per_object", "st", SCOPE_CITE, per_object="bedroom", catalog_id="uttag-dubbelt")
    (post,) = _posts([record])
    assert post.quantity is None and post.status == "Saknar mängd"
    assert post.basis == "? per sovrum × 3 sovrum"
    assert "Saknar mängd: ange antal per sovrum" in post.notes
    assert post.lines[0].record.record.citations[0].block_id == "fu-001" and post.rows


def test_a_complete_installation_becomes_the_recipe_room_by_room() -> None:
    whole = req("Komplett elinstallation", "per_object", "st", SCOPE_CITE, per_object="apartment")
    whole["complete_installation"] = True
    stated = req(
        "Bänkuttag",
        "per_object",
        "st",
        [cite("fu-002", "2 bänkuttag per kök")],
        quantity=2,
        per_object="kitchen",
        catalog_id="bankuttag",
    )
    posts = {p.name: p for p in _posts([whole, stated], TEST_RECIPE)}
    assert posts["Vägguttag dubbelt, sovrum"].quantity == 9
    assert posts["Vägguttag dubbelt, vardagsrum"].quantity == 15  # 5 × 3 living rooms
    assert posts["Takuttag, vardagsrum"].quantity == 6  # the recipe item matched by name
    assert posts["Takuttag, hallar"].quantity == 3  # one hall per apartment, from the recipe
    assert posts["Bänkuttag"].quantity == 6  # the stated count per kitchen wins over the recipe's
    assert "Bänkuttag, kök" not in posts
    assert not any("Saknas i katalogen" in name for name in posts)  # an unbound recipe item is never priced
    for name in ("Vägguttag dubbelt, sovrum", "Takuttag, hallar"):
        p = posts[name]
        assert p.mapped_by == "recept" and p.level == "Enligt recept" and p.status == "Prissatt"
        assert p.lines[0].record.record.citations[0].block_id == "fu-001" and p.rows


def test_a_complete_installation_without_a_recipe_is_one_post_missing_its_quantity() -> None:
    whole = req("Komplett elinstallation", "per_object", "st", SCOPE_CITE, per_object="apartment")
    whole["complete_installation"] = True
    (post,) = _posts([whole])
    assert post.status == "Saknar mängd" and post.basis == "? per lägenhet × 3 lägenheter"


def test_per_object_lines_for_different_room_types_are_separate_posts() -> None:
    def per(kind: str, n: int) -> dict:
        return req("Uttag", "per_object", "st", SCOPE_CITE, quantity=n, per_object=kind, catalog_id="uttag-dubbelt")

    posts = _posts([per("bedroom", 2), per("living_room", 4)])
    assert sorted(p.quantity for p in posts) == [6, 12]  # never a conflict between room types


def test_a_per_space_rule_plugs_in_as_a_labelled_post() -> None:
    rule = DerivedQuantity(
        name="Belysning cykelrum",
        unit="st",
        scope="included",
        quantity=Decimal(4),
        basis="2 per cykelrum × 2 cykelrum",
        label="regel per utrymme (test)",
        citations=(Citation(block_id="space-001", quote="Cykelrum"),),
        catalog_id="armatur-trapphus",
    )
    (post,) = [p for p in _posts([], derived=[rule]) if p.name == "Belysning cykelrum"]
    assert post.quantity == 4 and post.status == "Prissatt" and "Enligt regel per utrymme (test)" in post.notes


# --- recipes -----------------------------------------------------------------------------------------------------


def test_the_customer_recipe_wins_and_says_so(tmp_path: Path) -> None:
    assert load_recipe("co-001", tmp_path) is None
    default = tmp_path / "catalogs" / "estim-default-recipe.json"
    default.parent.mkdir(parents=True)
    default.write_text(json.dumps(TEST_RECIPE.model_dump(mode="json")))
    assert load_recipe("co-001", tmp_path).label == DEFAULT_LABEL
    customer = paths.company_dir("co-001", tmp_path) / "references" / "outlet-recipe.toml"
    customer.parent.mkdir(parents=True)
    customer.write_text('source = "invented"\n\n[[items]]\nroom = "bedroom"\nitem = "Vägguttag dubbelt"\ncount = 2\n')
    recipe = load_recipe("co-001", tmp_path)
    assert recipe.label == CUSTOMER_LABEL and recipe.source == "invented"
    assert bind(recipe, _catalog()).count_for("bedroom", "uttag-dubbelt").count == 2
    customer.write_text("items = 3\n")  # broken: an error, never a silent fallback to the default
    with pytest.raises(RecipeError) as err:
        load_recipe("co-001", tmp_path)
    assert "invented" not in str(err.value)


def test_a_customer_recipe_needs_its_source(tmp_path: Path) -> None:
    customer = paths.company_dir("co-001", tmp_path) / "references" / "outlet-recipe.toml"
    customer.parent.mkdir(parents=True)
    customer.write_text("[[items]]\nroom = 'bedroom'\nitem = 'x'\ncount = 1\n")
    with pytest.raises(RecipeError):
        load_recipe("co-001", tmp_path)


APP_SNIPPET = """
export const HALF_ROOM_EXAMPLE = { room: 'Halvrum' as const, sockets: 7, lamps: 2, example: true };
const EARLIER_EXAMPLE = { Vardagsrum: { sockets: 1, lamps: 1 } };
export const exampleRoomRecipes = { rooms: [
  {room:'Vardagsrum',sockets:8,lamps:2,example:true},{room:'Sovrum',sockets:6,lamps:2,example:true},
  { ...HALF_ROOM_EXAMPLE },{room:'Hall',sockets:5,lamps:1,example:true},{room:'Kök',sockets:9,lamps:0,example:true},
] };
"""


def test_the_exporter_reads_the_example_recipe_and_binds_items_by_name() -> None:
    assert parse_recipe_source(APP_SNIPPET)["Vardagsrum"] == (8, 2)  # the earlier example values are skipped
    catalog = Catalog.model_validate(
        {
            "name": "estim-default",
            "label": "x",
            "hourly_rate": 0,
            "items": [{"id": "s", "name": "Dubbelt vägguttag"}, {"id": "k", "name": "Bänkuttag"}],
        }
    )
    recipe = build(APP_SNIPPET, catalog)
    assert recipe.label == DEFAULT_LABEL and "exempelvärden" in recipe.source
    items = {(i.room, i.item): (i.catalog_id, i.count) for i in recipe.items}
    assert items[("kitchen", "Bänkuttag")] == ("k", 9)
    assert items[("bedroom", "Dubbelt vägguttag")] == ("s", 6)
    assert items[("bedroom", "Lamputtag")] == (None, 2)  # not in this catalog: written without an id
    assert ("kitchen", "Lamputtag") not in items  # zero is no item
    assert recipe.per_apartment == {"hall": 1, "kitchen": 1, "bathroom": 1, "living_room": 1}
    with pytest.raises(ValueError):
        parse_recipe_source(APP_SNIPPET.replace("Hall", "Entré"))


# --- end to end --------------------------------------------------------------------------------------------------

SCOPE_BLOCKS = [
    Block(id="aaaaaa-000001", doc_id="a" * 12, kind="heading", text="1 Lägenheter", page=1),
    Block(id="aaaaaa-000002", doc_id="a" * 12, kind="paragraph", text="Lägenheterna elinstalleras komplett.", page=1),
    Block(id="aaaaaa-000003", doc_id="a" * 12, kind="paragraph", text="Sovrum förses med takuttag.", page=1),
    Block(id="aaaaaa-000004", doc_id="a" * 12, kind="paragraph", text="Tvättstugan förses med uttag.", page=1),
    Block(id="bbbbbb-000001", doc_id="b" * 12, kind="sheet_row", text="Lgh | Typ | Yta", sheet="L", row=1),
    Block(id="bbbbbb-000002", doc_id="b" * 12, kind="sheet_row", text="1101 | 3 rok | 71", sheet="L", row=2),
    Block(id="bbbbbb-000003", doc_id="b" * 12, kind="sheet_row", text="1102 | 2 rok | 52", sheet="L", row=3),
]
SCOPE_OUTLINE = [{"doc_id": "a" * 12, "level": 1, "title": "1 Lägenheter", "block_id": "aaaaaa-000001", "page": 1}]
SCOPE_ANSWERS: dict[str, dict[str, Any]] = {
    "aaaaaa-000001": {
        "records": [
            req(
                "Komplett elinstallation",
                "per_object",
                "st",
                [cite("aaaaaa-000002", "elinstalleras komplett")],
                per_object="apartment",
                complete_installation=True,
            ),
            req(
                "Takuttag i sovrum",
                "per_object",
                "st",
                [cite("aaaaaa-000003", "Sovrum förses med takuttag")],
                per_object="bedroom",
                catalog_id="takuttag",
            ),
        ],
        "set_aside": [{"block_ids": ["aaaaaa-000004"], "reason": "common space, handled by per-space rules"}],
    },
    "bbbbbb-000001": {
        "records": [
            obj("apartment", "list_row", [cite("bbbbbb-000002", "1101")]),
            obj("apartment", "list_row", [cite("bbbbbb-000003", "1102")]),
        ],
        "set_aside": [],
    },
}


def _scope_handler(params: dict[str, Any]):
    if "tools" in params and params["tools"][0]["name"] == "submit_quantities":
        return message([submit(SCOPE_ANSWERS.get(first_block(params) or "", {"records": []}))])
    return good_handler(params)


@pytest.fixture
def scope_tender(data_root: Path) -> Path:
    tender = paths.dev_tender_dir("co-001", "t-0001")
    write_prep(tender / "prep", SCOPE_BLOCKS, SCOPE_OUTLINE)
    write_catalog(data_root, catalog=SCOPE_CATALOG)
    recipe = data_root / "catalogs" / "estim-default-recipe.json"
    recipe.write_text(json.dumps(TEST_RECIPE.model_dump(mode="json")))
    return tender


def test_a_run_prices_scope_passages_through_the_recipe(scope_tender: Path) -> None:
    summary = run_dev_tender("co-001", "t-0001", sdk=fake_sdk(_scope_handler), catalog_name="synthetic", run_id="k")
    assert summary["scope_lines"] == 3 and summary["scope_lines_not_handled"] == 1  # the set-aside common space
    assert summary["rooms_from_row_descriptors"] == 2 and summary["recipe"] == DEFAULT_LABEL
    assert summary["posts_from_recipe"] == 5 and summary["posts_missing_quantity"] == 1
    out = scope_tender / "kalkyl" / "k"
    result = json.loads((out / "result.json").read_text())
    assert result["recipe"]["label"] == DEFAULT_LABEL
    posts = {p["name"]: p for p in result["posts"]}
    bedroom = posts["Takuttag i sovrum"]  # the bedroom ceiling outlet: no count written, none in the recipe
    assert bedroom["status"] == "Saknar mängd" and bedroom["basis"] == "? per sovrum × 3 sovrum"
    sockets = posts["Vägguttag dubbelt, sovrum"]
    assert sockets["quantity"] == "9" and sockets["recipe"] == DEFAULT_LABEL
    assert sockets["object_rows"] == ["bbbbbb-000002", "bbbbbb-000003"]
    uncovered = {u["block_id"]: u for u in result["uncovered"]}
    assert uncovered["aaaaaa-000004"]["line"] == "scope" and uncovered["aaaaaa-000004"]["set_aside_by_model"]
