"""Search, the read tools and the shared prefix, on a synthetic corpus."""

from __future__ import annotations

import base64
from pathlib import Path

from granskning_fakes import CALC, DOC_AF, DOC_EL, corpus, png_bytes

from anbud.granskning.prefix import CACHE_1H, RULE, defuse, estimate_tokens, system_blocks
from anbud.granskning.search import SearchIndex
from anbud.granskning.toolbox import LAW_NOT_AVAILABLE, Toolbox


def test_search_ranks_words_and_finds_words_inside_compounds(tmp_path: Path) -> None:
    index = SearchIndex(corpus(tmp_path))
    assert index.search("etappvite")[0].block_id == "aaaaaa-000002"
    assert index.search("trapphus")[0].block_id == "bbbbbb-000002"  # prefix match on a word
    assert "bbbbbb-000003" in [h.block_id for h in index.search("laddplatser")]  # inside a compound, via trigrams
    hit = index.search("närvarostyrning")[0]
    assert "[" in hit.snippet and "]" in hit.snippet
    assert index.search("etappvite", doc_ids={DOC_EL}) == []
    assert index.search("  ") == []
    index.search('"AND OR (')  # FTS syntax in a query never raises
    outline_hits = index.search("gränsdragning")
    assert outline_hits and outline_hits[0].source == "outline" and outline_hits[0].block_id is None


def test_tools_return_ids_with_provenance(tmp_path: Path) -> None:
    box = Toolbox(corpus(tmp_path), CALC, tile_loader=lambda sha: png_bytes())
    out = box.run("search_blocks", {"query": "etappvite"})
    assert out.error is None and out.result_ids[0] == "aaaaaa-000002"
    assert out.content.startswith("<tender_corpus") and "aaaaaa-000002 p1 P:" in out.content

    box.set_roles({DOC_AF: "AF", DOC_EL: "teknisk beskrivning el"})
    assert box.run("search_blocks", {"query": "trapphus", "roles": ["af"]}).result_ids == []
    assert box.run("search_blocks", {"query": "trapphus", "roles": ["AF"]}).content.endswith("No blocks match.")
    assert "searched every document" in box.run("search_blocks", {"query": "x", "roles": ["nope"]}).content

    read = box.run("read_blocks", {"ids": ["bbbbbb-000004", "nope", "bbbbbb-000004"]})
    assert read.result_ids == ["bbbbbb-000004"] and "Unknown block ids: nope" in read.content
    assert read.error is None
    assert box.run("read_blocks", {"ids": ["nope"]}).error == "unknown_ids"

    outline = box.run("outline", {"doc_id": DOC_EL[:6]})
    assert outline.result_ids == ["bbbbbb-000001"] and "6.3 Belysning" in outline.content
    assert box.run("outline", {"doc_id": "zzzzzz"}).error == "unknown_doc"
    assert len(box.run("outline", {}).result_ids) == 2

    posts = box.run("calc_posts", {"filter": "kabelstege"})
    assert posts.result_ids == ["c00000000-s1-r6"] and "qty 0 m" in posts.content
    assert len(box.run("calc_posts", {}).result_ids) == 2

    law = box.run("law_lookup", {"source": "PBF", "section": "3:22"})
    assert law.error == "not_available" and law.content == LAW_NOT_AVAILABLE

    tile = box.run("view_tile", {"sheet": "cccccc", "tile": "p1-t1"})
    assert tile.error is None and tile.result_ids == ["cccccc-p1-t1"]
    caption, image = tile.content
    assert caption["type"] == "text" and image["type"] == "image"
    assert base64.b64decode(image["source"]["data"]) == png_bytes()
    assert box.run("view_tile", {"sheet": "cccccc-whatever", "tile": "cccccc-p1-t1"}).error is None
    assert box.run("view_tile", {"sheet": "cccccc", "tile": "p9-t9"}).error == "unknown_tile"

    assert box.run("search_blocks", {"query": ""}).error == "invalid_input"
    assert box.run("submit_flags", {"flags": []}).error == "unknown_tool"


def test_tender_text_cannot_close_the_corpus_delimiter(tmp_path: Path) -> None:
    box = Toolbox(corpus(tmp_path), CALC)
    out = box.run("read_blocks", {"ids": ["aaaaaa-000004"]})
    assert out.content.count("</tender_corpus>") == 1 and "‹/tender_corpus>" in out.content
    assert defuse("<calculation>") == "‹calculation>" and defuse("a < b") == "a < b"


def test_system_blocks_are_byte_identical_with_one_1h_breakpoint(tmp_path: Path) -> None:
    c = corpus(tmp_path)
    blocks = system_blocks(c, CALC)
    assert blocks == system_blocks(corpus(tmp_path / "again"), list(CALC))
    assert [b.get("cache_control") for b in blocks] == [None, None, None, CACHE_1H]
    brief, rule, corpus_text, calc_text = (b["text"] for b in blocks)
    assert "contract_vite" in brief and "<tender_corpus>" in rule and rule == RULE
    assert corpus_text.startswith("<tender_corpus>") and corpus_text.endswith("</tender_corpus>")
    assert corpus_text.count("</tender_corpus>") == 1  # the planted closing tag is defused
    assert "aaaaaa-000002 p1 P: Etappvite" in corpus_text
    assert "tiles=cccccc-p1-t1" in corpus_text and '"E-plan.pdf"' in corpus_text
    assert "c00000000-s1-r6 [Belysning] Kabelstege | qty 0 m | à 250 | sum 0 | 0 h" in calc_text
    assert estimate_tokens(blocks) > 0
    assert "No calculation" in system_blocks(c, [])[3]["text"]
