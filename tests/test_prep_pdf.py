from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from synthetic_docs import A2, A3_LANDSCAPE, Page, Text, make_pdf

from anbud.prep import pdf as P
from anbud.prep.build import DocBuilder
from anbud.store import RunStore


def header_footer(n: int, total: int) -> list[Text]:
    return [Text(60, 20, "Projekt Exempelhuset, rambeskrivning", 9), Text(60, 810, f"Sida {n} av {total}", 9)]


def tender_pdf() -> bytes:
    body = [
        Text(60, 90, "Förfrågningsunderlag", 18, bold=True),
        Text(60, 140, "1.1 Omfattning", 12, bold=True),
        Text(60, 165, "Entreprenaden omfattar ny elinstallation i samtliga", 10),
        Text(60, 177, "lägenheter och allmänna utrymmen enligt denna beskrivning.", 10),
        Text(60, 205, "Arbetet utförs i bebodda hus och ska samordnas med boende.", 10),
        Text(60, 235, "Kabel", 10),
        Text(300, 235, "120 m", 10),
        Text(60, 260, "• Ny servisledning", 10),
        Text(60, 272, "• Nya lägenhetscentraler", 10),
        Text(60, 300, "ABC.12 Allmänt om arbetena", 10),
        Text(60, 325, "Frågor till b.exempel@example.se, Tel: 08-111 222 33.", 10),
    ]
    pages = [Page(texts=[*header_footer(1, 4), *body])]
    for n in (2, 3, 4):
        pages.append(
            Page(texts=[*header_footer(n, 4), Text(60, 300, f"Text på sidan {n} om arbetenas utförande.", 10)])
        )
    return make_pdf(pages)


def read(data: bytes, tmp_path: Path, company: str = "co-001") -> tuple[DocBuilder, list, list, int]:
    sha = hashlib.sha256(data).hexdigest()
    builder = DocBuilder(sha, sha[:12])
    store = RunStore(company, tmp_path / "data")
    pages, tiles, dropped = P.read_pdf(data, builder, store.put_blob)
    return builder, pages, tiles, dropped


def test_headings_paragraphs_tables_and_bullets(tmp_path: Path) -> None:
    builder, pages, tiles, _ = read(tender_pdf(), tmp_path)
    by_text = {b.text: b for b in builder.blocks}
    assert by_text["Förfrågningsunderlag"].kind == "heading"
    assert by_text["1.1 Omfattning"].kind == "heading"
    assert by_text["ABC.12 Allmänt om arbetena"].kind == "heading"
    joined = (
        "Entreprenaden omfattar ny elinstallation i samtliga lägenheter och allmänna utrymmen enligt denna beskrivning."
    )
    assert by_text[joined].kind == "paragraph"
    assert by_text["Arbetet utförs i bebodda hus och ska samordnas med boende."].kind == "paragraph"
    assert by_text["Kabel | 120 m"].kind == "table_row"
    assert "• Ny servisledning" in by_text and "• Nya lägenhetscentraler" in by_text
    assert [p.kind for p in pages] == ["text"] * 4 and tiles == []


def test_outline_levels_and_numbers(tmp_path: Path) -> None:
    builder, *_ = read(tender_pdf(), tmp_path)
    entries = {e.title: e for e in builder.outline}
    assert entries["1.1 Omfattning"].number == "1.1" and entries["1.1 Omfattning"].level == 2
    assert entries["ABC.12 Allmänt om arbetena"].number == "ABC.12"
    assert entries["Förfrågningsunderlag"].level == 1
    assert all(e.block_id in {b.id for b in builder.blocks} for e in builder.outline)


def test_page_furniture_is_kept_once(tmp_path: Path) -> None:
    builder, _, _, dropped = read(tender_pdf(), tmp_path)
    texts = [b.text for b in builder.blocks]
    assert texts.count("Projekt Exempelhuset, rambeskrivning") == 1
    assert sum(t.startswith("Sida ") for t in texts) == 1
    assert dropped == 6


def test_personal_data_is_masked_with_offsets(tmp_path: Path) -> None:
    builder, *_ = read(tender_pdf(), tmp_path)
    block = next(b for b in builder.blocks if "Frågor till" in b.text)
    assert block.text == "Frågor till [e-post], Tel: [telefon]."
    assert sorted(m.kind for m in builder.masked) == ["email", "phone"]
    for m in builder.masked:
        assert block.text[m.start : m.end].startswith("[")


def test_block_ids_are_stable_and_carry_the_file_prefix(tmp_path: Path) -> None:
    data = tender_pdf()
    first, *_ = read(data, tmp_path / "one")
    second, *_ = read(data, tmp_path / "two")
    assert [b.id for b in first.blocks] == [b.id for b in second.blocks]
    sha = hashlib.sha256(data).hexdigest()
    assert all(b.id.startswith(sha[:6] + "-") for b in first.blocks)
    assert len({b.id for b in first.blocks}) == len(first.blocks)


def test_repeated_text_gets_numbered_ids(tmp_path: Path) -> None:
    pages = [Page(texts=[Text(60, 300, "Se ritning för detaljer om utförandet.", 10)]) for _ in range(2)]
    builder, *_ = read(make_pdf(pages), tmp_path)
    ids = [b.id for b in builder.blocks]
    assert len(ids) == 2 and ids[1] == ids[0] + "-2"


def test_a_scanned_page_is_tiled_within_limits(tmp_path: Path) -> None:
    builder, pages, tiles, _ = read(make_pdf([Page(size=A2, image=(240, 340))]), tmp_path)
    assert pages[0].kind == "scan" and pages[0].tiles == len(tiles) == 2
    assert builder.blocks == []
    store = RunStore("co-001", tmp_path / "data")
    for tile in tiles:
        assert tile.x1 - tile.x0 <= P.TILE_MAX and tile.y1 - tile.y0 <= P.TILE_MAX
        assert store.get_blob(tile.blob).startswith(b"\x89PNG")
        assert tile.reason == "scan" and tile.dpi == 150
    overlap = tiles[0].y1 - tiles[1].y0
    assert overlap >= P.TILE_OVERLAP * P.TILE_MAX


def test_tiles_are_byte_identical_across_runs(tmp_path: Path) -> None:
    data = make_pdf([Page(size=A2, image=(240, 340))])
    _, _, one, _ = read(data, tmp_path / "one")
    _, _, two, _ = read(data, tmp_path / "two")
    assert [t.blob for t in one] == [t.blob for t in two]


def test_a_vector_drawing_is_tiled_and_its_labels_stay_separate(tmp_path: Path) -> None:
    labels = [Text(100, 100, "Central C1", 8), Text(500, 100, "Belysning korridor", 8)]
    page = Page(size=A3_LANDSCAPE, vector_lines=300, texts=labels)
    builder, pages, tiles, _ = read(make_pdf([page]), tmp_path)
    assert pages[0].kind == "drawing"
    assert tiles and all(t.reason == "drawing" for t in tiles)
    assert {b.text for b in builder.blocks} == {"Central C1", "Belysning korridor"}


def test_a_blank_page_gets_no_tiles(tmp_path: Path) -> None:
    _, pages, tiles, _ = read(make_pdf([Page()]), tmp_path)
    assert pages[0].kind == "blank" and tiles == []


def test_a_short_stamp_becomes_a_block_and_reaches_the_gate(tmp_path: Path) -> None:
    builder, pages, _, _ = read(make_pdf([Page(texts=[Text(60, 60, "HEMLIG", 14)])]), tmp_path)
    assert pages[0].kind == "text" and [b.text for b in builder.blocks] == ["HEMLIG"]
    assert [m.kind for m in builder.markings] == ["hemlig"]
    scan = make_pdf([Page(size=A2, image=(120, 170), texts=[Text(60, 60, "HEMLIG", 14)])])
    builder, pages, tiles, _ = read(scan, tmp_path / "scan")
    assert pages[0].kind == "scan" and tiles and [m.kind for m in builder.markings] == ["hemlig"]


def test_a_rotated_page_reads_in_display_order(tmp_path: Path) -> None:
    texts = [
        Text(60, 90, "Rubrik på en liggande sida", 16, bold=True),
        Text(60, 130, "Texten på den liggande sidan löper från vänster till höger.", 10),
    ]
    builder, pages, _, _ = read(make_pdf([Page(rotate=90, texts=texts)]), tmp_path)
    assert (pages[0].width, pages[0].height) == (842.0, 595.0)
    assert [(b.kind, b.text) for b in builder.blocks] == [
        ("heading", "Rubrik på en liggande sida"),
        ("paragraph", "Texten på den liggande sidan löper från vänster till höger."),
    ]
    box = builder.blocks[1].box
    assert box is not None and 55 < box.x0 < 65 and 125 < box.y0 < 135  # display coordinates


def test_a_soft_hyphen_joins_the_word_across_lines(tmp_path: Path) -> None:
    texts = [
        Text(60, 100, "Entreprenaden omfattar installation av belysnings-", 10),
        Text(60, 112, "armaturer i alla trapphus och korridorer.", 10),
    ]
    builder, _, _, _ = read(make_pdf([Page(texts=texts)]), tmp_path)
    assert [b.text for b in builder.blocks] == [
        "Entreprenaden omfattar installation av belysningsarmaturer i alla trapphus och korridorer."
    ]


def test_a_title_block_with_changing_drawing_numbers_is_not_furniture(tmp_path: Path) -> None:
    pages = [
        Page(texts=[Text(60, 300, f"Planritning plan {n}", 10), Text(400, 800, f"Ritning E-10{n + 4}", 9)])
        for n in range(1, 5)
    ]
    builder, _, _, dropped = read(make_pdf(pages), tmp_path)
    assert dropped == 0
    assert sum(b.text.startswith("Ritning E-10") for b in builder.blocks) == 4


def test_personal_data_is_painted_over_on_drawing_tiles(tmp_path: Path) -> None:
    from PIL import Image

    labels = [Text(100, 100, "Central C1", 8), Text(100, 400, "Kontakt Tel: 08-111 222 33", 30)]
    page = Page(size=A3_LANDSCAPE, vector_lines=300, texts=labels)
    builder, _, tiles, _ = read(make_pdf([page]), tmp_path)
    assert any(b.text == "Kontakt Tel: [telefon]" for b in builder.blocks)
    store = RunStore("co-001", tmp_path / "data")
    image = Image.open(io.BytesIO(store.get_blob(tiles[0].blob))).convert("L")
    scale = tiles[0].dpi / 72
    phone_x = 100 + 13 * 30 * 0.6  # roughly where the digits sit, after "Kontakt Tel: "
    darkness = [image.getpixel((round((phone_x + dx) * scale), round(418 * scale))) for dx in range(0, 120, 10)]
    assert max(darkness) < 30  # a black box, not white paper with digits
    label_row = [image.getpixel((round((100 + dx) * scale), round(104 * scale))) for dx in range(0, 40, 2)]
    assert max(label_row) > 200  # the unmasked label is not painted over


@pytest.mark.parametrize("width,height", [(100, 100), (2576, 2576), (2577, 900), (4962, 3508), (9933, 7016)])
def test_tile_grid_covers_the_image_with_overlap(width: int, height: int) -> None:
    tiles = P.tile_grid(width, height)
    xs = sorted({(x0, x1) for x0, _, x1, _ in tiles})
    ys = sorted({(y0, y1) for _, y0, _, y1 in tiles})
    for spans, total in ((xs, width), (ys, height)):
        assert spans[0][0] == 0 and min(spans[-1][1], total) == total
        for (_, a1), (b0, _) in zip(spans, spans[1:], strict=False):
            assert a1 - b0 >= P.TILE_OVERLAP * P.TILE_MAX
        assert all(min(s1, total) - s0 <= P.TILE_MAX for s0, s1 in spans)


def test_render_dpi_lowers_a_little_to_save_tiles() -> None:
    a3 = (1191.0, 842.0)
    dpi = P.render_dpi(*a3, native=300)
    assert 270 <= dpi < 300
    assert len(P.tile_grid(round(a3[0] / 72 * dpi), round(a3[1] / 72 * dpi))) == 4
    assert P.render_dpi(*a3, native=None) <= P.VECTOR_DPI
    assert P.render_dpi(*a3, native=72) >= int(P.DPI_RANGE[0] * (1 - P.DPI_SLACK))
