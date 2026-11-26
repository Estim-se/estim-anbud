"""Kalkyl's code steps on synthetic data: quantity lines, chunks, citation checks, the catalog, objects, posts, kronor
and coverage. No model, $0."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from kalkyl_synthetic import CATALOG, PRICE_STRINGS, cite, obj, req, write_catalog, write_prep

from anbud.granskning.corpus import Corpus
from anbud.kalkyl.catalog import CatalogError, load, prompt_lines
from anbud.kalkyl.coverage import uncovered_lines
from anbud.kalkyl.extract import ChunkResult
from anbud.kalkyl.lines import Chunk, heading_paths, is_quantity_line, make_chunks, numbers_in
from anbud.kalkyl.price import (
    build_posts,
    calc_posts,
    count_objects,
    price_posts,
    totals_of,
    unit_family,
)
from anbud.kalkyl.records import QuantityRecord, Rejected, SetAside, VerifiedRecord
from anbud.kalkyl.verify import check_record, level_of
from anbud.models import Block


@pytest.fixture
def corpus(tmp_path: Path) -> Corpus:
    return Corpus.load(write_prep(tmp_path / "prep"))


@pytest.fixture
def catalog(tmp_path: Path):
    write_catalog(tmp_path)
    return load("synthetic", tmp_path)


def _block(kind: str, text: str) -> Block:
    return Block(id="x", doc_id="d", kind=kind, text=text)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kind", "text", "expected"),
    [
        ("table_row", "L1 | Downlight | 24 st", True),
        ("sheet_row", "1001 | 3 | 1 | 1", True),
        ("sheet_row", "Lgh | Rum | Kök", False),
        ("table_row", "Typ L12 | Armatur", False),
        ("paragraph", "Kabelstege 45 m i källare.", True),
        ("paragraph", "Totalt 1 200 m² golv.", True),
        ("paragraph", "Handlingen är daterad 2026-01-15.", False),
        ("paragraph", "Säkring 16 A och kabel 2,5 mm²", False),
        ("heading", "2 Installationer 4 st", False),
        ("tile_text", "Uttag 3 st", True),
    ],
)
def test_quantity_lines_are_found_by_form(kind: str, text: str, expected: bool) -> None:
    assert is_quantity_line(_block(kind, text)) is expected


def test_numbers_in_reads_grouping_and_decimal_commas_and_skips_codes() -> None:
    assert numbers_in("L12 typ: 1 200 st och 2,5 m, 45 m2") == {1200.0, 1.0, 200.0, 2.5, 45.0}


def test_heading_paths_follow_the_outline(corpus: Corpus) -> None:
    paths = heading_paths(corpus)
    assert paths["aaaaaa-000005"] == ("2 Installationer",)
    assert paths["aaaaaa-000010"] == ("3 Option",)
    assert paths["bbbbbb-000003"] == ("Lägenheter",)


def test_chunks_stay_in_one_document_and_respect_the_caps(corpus: Corpus) -> None:
    whole = make_chunks(corpus, 12000, 30)
    assert [c.doc_id for c in whole] == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]
    assert sum(len(c.block_ids) for c in whole) == len(corpus.order)
    small = make_chunks(corpus, 10_000, 2)
    assert all(len(c.quantity_ids) <= 2 for c in small)
    assert [b for c in small for b in c.block_ids] == corpus.order  # every block once, in reading order
    mid_sheet = [c for c in small if c.doc_id == "bbbbbbbbbbbb" and c.block_ids[0] != "bbbbbb-000001"]
    assert mid_sheet and all(c.context_ids == ["bbbbbb-000001"] for c in mid_sheet)  # the column headings come along


def test_a_cut_moves_back_to_a_heading(corpus: Corpus) -> None:
    chunks = make_chunks(corpus, 10_000, 5)  # four quantity lines and one scope line fill the first chunk
    starts = [c.block_ids[0] for c in chunks if c.doc_id == "aaaaaaaaaaaa"]
    assert "aaaaaa-000009" in starts  # the option heading starts a chunk


def _chunk(corpus: Corpus) -> Chunk:
    return make_chunks(corpus, 12000, 30)[0]


def _record(**kw) -> QuantityRecord:
    return QuantityRecord.model_validate(kw)


@pytest.mark.parametrize(
    ("record", "problem"),
    [
        (req("Downlight", "stated", "st", [cite("aaaaaa-000005", "Downlight | 24 st")], quantity=24), None),
        (req("Downlight", "stated", "st", [cite("aaaaaa-000005", "Downlight 24 st")], quantity=24), "isn't in block"),
        (req("Downlight", "stated", "st", [cite("aaaaaa-000005", "Downlight | 24 st")], quantity=25), "isn't written"),
        (req("Uttag", "stated", "st", [cite("bbbbbb-000002", "1001")], quantity=1001), "isn't in this chunk"),
        (req("Okänd", "stated", "st", [cite("zzzzzz-000001", "24")], quantity=24), "isn't in this chunk"),
        (req("Central", "per_object", "st", [cite("aaaaaa-000008", "i varje lägenhet")], quantity=1), "per_object"),
        (
            req(
                "Central",
                "per_object",
                "st",
                [cite("aaaaaa-000008", "i varje lägenhet")],
                quantity=3,
                per_object="apartment",
            ),
            "per object 3",
        ),
        (
            req(
                "Central",
                "per_object",
                "st",
                [cite("aaaaaa-000008", "i varje lägenhet")],
                quantity=1,
                per_object="apartment",
            ),
            None,
        ),
        (req("Central", "missing", "st", [cite("aaaaaa-000008", "lägenhetscentral")], quantity=2), "can't carry"),
        (req("Uttag", "list_row", "st", [cite("aaaaaa-000004", "dubbla")]), "list_row is for object"),
        ({**obj("apartment", "stated", [cite("aaaaaa-000002", "tre trapphus")]), "object_kind": None}, "object_kind"),
        (req("Trapphus", "stated", "st", [cite("aaaaaa-000002", "")], quantity=3), "empty"),
    ],
)
def test_citations_and_quantities_are_checked(corpus: Corpus, record: dict, problem: str | None) -> None:
    reason = check_record(_record(**record), _chunk(corpus), corpus)
    assert (reason is None) if problem is None else (problem in (reason or ""))


def test_a_quote_matches_after_normalisation(corpus: Corpus) -> None:
    quote = "Varje  lägenhet förses med 2 st dubbla vägguttag"  # double space and a no-break space
    record = _record(**req("Uttag", "stated", "st", [cite("aaaaaa-000004", quote)], quantity=2))
    assert check_record(record, _chunk(corpus), corpus) is None
    assert level_of(record, corpus) == "exact"


def test_a_block_without_text_never_accepts_a_quote(tmp_path: Path) -> None:
    blocks = [Block(id="aaaaaa-000001", doc_id="aaaaaaaaaaaa", kind="paragraph", text=" ")]
    corpus = Corpus.load(write_prep(tmp_path / "p", blocks, []))
    chunk = Chunk(id="c001", doc_id="aaaaaaaaaaaa", block_ids=["aaaaaa-000001"])
    record = _record(**req("X", "lump_sum", "st", [cite("aaaaaa-000001", " ")]))
    assert check_record(record, chunk, corpus) is not None


def test_the_catalog_prompt_view_carries_no_prices(catalog) -> None:
    view = prompt_lines(catalog)
    assert "uttag-dubbelt | Vägguttag dubbelt | st | Uttag" in view
    assert not any(p in view for p in PRICE_STRINGS)


def test_a_bad_or_missing_catalog_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CatalogError):
        load("missing", tmp_path)
    with pytest.raises(CatalogError):
        load("../escape", tmp_path)
    write_catalog(tmp_path, "dup", {**CATALOG, "items": CATALOG["items"] + CATALOG["items"][:1]})
    with pytest.raises(CatalogError):
        load("dup", tmp_path)


def _verified(records: list[dict], level: str = "exact") -> list[VerifiedRecord]:
    out = []
    for n, raw in enumerate(records, 1):
        r = QuantityRecord.model_validate(raw)
        out.append(
            VerifiedRecord(
                id=f"c001-r{n:03d}", chunk_id="c001", record=r, level=level, block_ids=[c.block_id for c in r.citations]
            )
        )
    return out


def _apartments(n: int) -> list[dict]:
    return [obj("apartment", "list_row", [cite(f"bbbbbb-{i:06d}", "x")]) for i in range(1, n + 1)]


def test_objects_count_list_rows_once_and_flag_a_disagreeing_total() -> None:
    rows = _apartments(3) + [obj("apartment", "list_row", [cite("bbbbbb-000001", "x")])]  # the same row twice
    agree = count_objects(_verified(rows + [obj("apartment", "stated", [cite("t", "3")], quantity=3)]))
    assert agree["apartment"].count == 3 and agree["apartment"].conflict is None
    differ = count_objects(_verified(rows + [obj("apartment", "stated", [cite("t", "5")], quantity=5)]))
    assert differ["apartment"].count == 3 and "Konflikt" in (differ["apartment"].conflict or "")
    parts = [
        obj("apartment", "stated", [cite("t1", "10")], quantity=10, location="hus A"),
        obj("apartment", "stated", [cite("t2", "12")], quantity=12, location="hus B"),
        obj("apartment", "stated", [cite("t3", "22")], quantity=22),
    ]
    assert count_objects(_verified(parts))["apartment"].count == 22  # parts add up and agree with the total
    excluded = [obj("premises", "stated", [cite("t", "4")], quantity=4, scope="excluded")]
    assert "premises" not in count_objects(_verified(excluded))


def test_posts_multiply_per_object_merge_duplicates_and_add_additive(catalog) -> None:
    records = _apartments(3) + [
        req(
            "Uttag",
            "per_object",
            "st",
            [cite("a1", "2 st")],
            quantity=2,
            per_object="apartment",
            catalog_id="uttag-dubbelt",
        ),
        req("Downlight", "stated", "st", [cite("a2", "24 st")], quantity=24, catalog_id="downlight", type_code="L1"),
        req("Downlight", "stated", "st", [cite("a3", "24 st")], quantity=24, catalog_id="downlight", type_code="L1"),
        req(
            "Downlight extra",
            "stated",
            "st",
            [cite("a4", "6 st")],
            quantity=6,
            catalog_id="downlight",
            type_code="L1",
            additive=True,
        ),
        req("Kabelstege", "stated", "m", [cite("a5", "45 m")], quantity=45, catalog_id="kabelstege"),
        req("Kabelstege", "stated", "m", [cite("a6", "40 m")], quantity=40, catalog_id="kabelstege"),
    ]
    verified = _verified(records)
    posts = {p.catalog_id: p for p in build_posts(verified, count_objects(verified), catalog)}
    assert posts["uttag-dubbelt"].quantity == Decimal(6)
    assert "2 per lägenhet × 3 lägenheter" in posts["uttag-dubbelt"].basis
    assert posts["downlight"].quantity == Decimal(30)  # 24 stated twice counts once, plus 6 on top
    assert posts["kabelstege"].quantity == Decimal(40) and any("Konflikt" in n for n in posts["kabelstege"].notes)


def test_a_per_object_post_without_objects_is_missing_a_quantity(catalog) -> None:
    verified = _verified(
        [
            req(
                "Uttag",
                "per_object",
                "st",
                [cite("a1", "2 st")],
                quantity=2,
                per_object="kitchen",
                catalog_id="uttag-dubbelt",
            )
        ]
    )
    (post,) = build_posts(verified, count_objects(verified), catalog)
    price_posts([post], catalog)
    assert post.quantity is None and post.status == "Saknar mängd"
    assert any("kök saknas" in n for n in post.notes)


def test_a_catalog_item_in_another_unit_is_refused_not_priced(catalog) -> None:
    verified = _verified([req("Stege", "stated", "st", [cite("a1", "4 st")], quantity=4, catalog_id="kabelstege")])
    (post,) = build_posts(verified, {}, catalog)
    price_posts([post], catalog)
    assert post.catalog_id is None and post.status == "Saknar pris"
    assert "passar inte" in (post.mapping_note or "")


def test_kronor_are_decimal_and_options_and_other_trades_stay_out_of_the_total(catalog) -> None:
    records = [
        req("Uttag", "stated", "st", [cite("a1", "3 st")], quantity=3, catalog_id="uttag-dubbelt"),
        req("Oklart", "stated", "st", [cite("a2", "1 st")], quantity=1, catalog_id="downlight", scope="unclear"),
        req("Laddstolpe", "stated", "st", [cite("a3", "2 st")], quantity=2, catalog_id="laddstolpe", scope="option"),
        req("Rör", "stated", "m", [cite("a4", "30 m")], quantity=30, scope="other_trade", catalog_id="kabelstege"),
        req("Ej", "stated", "st", [cite("a5", "1 st")], quantity=1, scope="excluded", catalog_id="downlight"),
    ]
    posts = build_posts(_verified(records), {}, catalog)
    price_posts(posts, catalog)
    by = {p.name: p for p in posts}
    uttag = by["Uttag"]
    assert (uttag.material, uttag.hours, uttag.labour, uttag.total) == (
        Decimal("256.50"),
        Decimal("1.35"),
        Decimal("877.50"),
        Decimal("1134.00"),
    )
    assert by["Rör"].status == "Utförs av annan" and by["Rör"].total is None
    assert by["Ej"].status == "Ingår ej" and by["Ej"].total is None
    t = totals_of(posts)
    unclear = Decimal("420") + Decimal("0.80") * 650
    assert t.total == Decimal("1134.00") + unclear and t.unclear_total == unclear
    assert t.options_total == Decimal("24000") + Decimal("12.00") * 650
    contract = calc_posts(posts, {p.id: n for n, p in enumerate(posts, 5)})
    assert {c.text for c in contract} == {"Uttag", "Oklart", "Laddstolpe"}
    assert next(c for c in contract if c.text == "Laddstolpe").section == "Option"


def test_unit_families() -> None:
    assert unit_family("st") == unit_family("lgh") == unit_family("St.") == "count"
    assert unit_family("m²") == unit_family("kvm") == "area"
    assert unit_family("m") != unit_family("m2")


def test_every_quantity_line_is_used_or_listed_with_a_reason(corpus: Corpus) -> None:
    chunks = make_chunks(corpus, 12000, 30)
    used = _verified([req("Downlight", "stated", "st", [cite("aaaaaa-000005", "24 st")], quantity=24)])
    results = [
        ChunkResult(
            "c001",
            "submitted",
            set_aside=[SetAside(block_ids=["aaaaaa-000011"], kind="other_trade", reason="other trade")],
            rejected=[Rejected(chunk_id="c001", reason="record 2: quote", block_ids=["aaaaaa-000007"])],
        ),
        ChunkResult("c002", "not_run", error="budget"),
    ]
    listed = {u.block_id: u for u in uncovered_lines(chunks, results, used)}
    quantity_lines = {b for c in chunks for b in c.quantity_ids}
    scope_lines = {b for c in chunks for b in c.scope_ids}
    assert scope_lines == {"aaaaaa-000008"}
    assert set(listed) == (quantity_lines | scope_lines) - {"aaaaaa-000005"}  # nothing silently dropped
    assert listed["aaaaaa-000008"].line == "scope" and "Omfattningstext" in listed["aaaaaa-000008"].reason
    assert listed["aaaaaa-000011"].set_aside_by_model
    assert listed["aaaaaa-000007"].reason.startswith("Citatet underkändes")
    assert listed["aaaaaa-000004"].reason.startswith("Ej omhändertaget")
    assert listed["bbbbbb-000002"].reason == "Ej läst: körningens budget tog slut"


def test_a_list_row_counts_what_each_object_holds(corpus: Corpus) -> None:
    chunk = make_chunks(corpus, 12000, 30)[1]
    row = obj(
        "apartment",
        "list_row",
        [cite("bbbbbb-000002", "1001 | 3 | 1 | 1")],
        contains=[{"object_kind": "room", "quantity": 3}, {"object_kind": "kitchen", "quantity": 1}],
    )
    assert check_record(_record(**row), chunk, corpus) is None
    unwritten = {**row, "contains": [{"object_kind": "room", "quantity": 5}]}
    assert "isn't written" in (check_record(_record(**unwritten), chunk, corpus) or "")
    on_requirement = req("X", "stated", "st", [cite("bbbbbb-000002", "3")], quantity=3, contains=row["contains"])
    assert "object records" in (check_record(_record(**on_requirement), chunk, corpus) or "")
    second = obj(
        "apartment",
        "list_row",
        [cite("bbbbbb-000003", "1002 | 2 | 1 | 1")],
        contains=[{"object_kind": "room", "quantity": 2}, {"object_kind": "kitchen", "quantity": 1}],
    )
    total = obj(
        "apartment", "stated", [cite("t", "2 lgh")], quantity=2, contains=[{"object_kind": "kitchen", "quantity": 1}]
    )
    objects = count_objects(_verified([row, second, total]))
    assert (objects["apartment"].count, objects["room"].count, objects["kitchen"].count) == (2, 5, 2)
    assert objects["kitchen"].conflict is None  # two rows with one kitchen each agree with 2 apartments of one kitchen


def test_only_unclear_set_asides_and_unhandled_lines_stay_unresolved() -> None:
    from anbud.kalkyl.records import Uncovered

    assert Uncovered(block_id="a", reason="r").unresolved
    assert Uncovered(block_id="a", reason="r", set_aside_by_model=True, set_aside_kind="unclear").unresolved
    assert not Uncovered(block_id="a", reason="r", set_aside_by_model=True, set_aside_kind="other_trade").unresolved
