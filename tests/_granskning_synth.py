"""A small synthetic tender for the Granskning tests. Every text here is written for the tests; nothing is real."""

from __future__ import annotations

from typing import Any

from anbud.granskning.contracts import CalcPost, LensResult, ToolCall
from anbud.granskning.corpus import Corpus
from anbud.granskning.kronor import Catalog, CatalogItem
from anbud.models import Block, Citation, Flag, FormulaInput, MoneyBasis
from anbud.prep.model import Tile

BLOCKS = [
    Block(id="aaa111-000001", doc_id="docA", kind="heading", text="3.2 Elinstallationer", page=4),
    Block(
        id="aaa111-000002",
        doc_id="docA",
        kind="paragraph",
        text="Entreprenören ska utföra all elin- stallation i trapphusen, totalt 24 armaturer.",
        page=4,
    ),
    Block(
        id="aaa111-000003",
        doc_id="docA",
        kind="paragraph",
        text="Vite utgår med 5 000 kr per påbörjad vecka vid försening av slutdagen.",
        page=5,
    ),
    Block(id="aaa111-000004", doc_id="docA", kind="paragraph", text="Byggström tillhandahålls av beställaren.", page=5),
    Block(id="bbb222-000001", doc_id="docB", kind="sheet_row", text="Rum 101 | Uttag | 12 st", sheet="Mängder", row=3),
    Block(id="bbb222-000002", doc_id="docB", kind="sheet_row", text="Rum 102 | Uttag | 8 st", sheet="Mängder", row=4),
    Block(id="ccc333-000001", doc_id="docC", kind="tile_text", text="Planritning plan 2", page=1),
]

DOCUMENTS = [
    {"doc_id": "docA", "kind": "pdf", "paths": ["inbox/Beskrivning.pdf"], "skipped": None, "pages": []},
    {"doc_id": "docB", "kind": "xlsx", "paths": ["inbox/Mangder.xlsx"], "skipped": None, "pages": []},
    {"doc_id": "docC", "kind": "pdf", "paths": ["inbox/Ritning.pdf"], "skipped": None, "pages": [{"kind": "drawing"}]},
    {"doc_id": "docD", "kind": "dwg", "paths": ["inbox/Underlag.dwg"], "skipped": "unsupported", "pages": []},
]

TILES = [
    Tile(id="tile-1", doc_id="docC", page=1, index=0, reason="drawing", dpi=150, x0=0, y0=0, x1=10, y1=10, blob="x")
]


def corpus() -> Corpus:
    return Corpus(
        blocks={b.id: b for b in BLOCKS},
        order=[b.id for b in BLOCKS],
        tiles={t.id: t for t in TILES},
        documents=DOCUMENTS,
    )


def catalog(default: bool = True, hourly_rate: float | None = 500.0) -> Catalog:
    return Catalog(
        name="synthetic",
        label="Syntetisk katalog",
        default=default,
        hourly_rate=hourly_rate,
        items=[
            CatalogItem(id="i1", name="Armatur trapphus", hours=1.0, material=1000.0),
            CatalogItem(id="i2", name="Vägguttag", hours=0.5, material=100.0),
            CatalogItem(id="i3", name="Byggström per etapp", hours=20.0, material=8000.0),
        ],
    )


CALC_POSTS = [
    CalcPost(id="calc-1", sheet="Kalkyl", row=10, text="Vägguttag", quantity=10, unit="st", amount=4000.0),
    CalcPost(id="calc-2", sheet="Kalkyl", row=11, text="Armatur trapphus", quantity=20, unit="st", unit_price=1500.0),
]


def basis(**kw: Any) -> MoneyBasis:
    kw.setdefault("kind", "missing_post")
    if "formula_inputs" in kw:
        kw["formula_inputs"] = [FormulaInput(name=k, value=v) for k, v in kw["formula_inputs"].items()]
    return MoneyBasis(**kw)


def flag(
    lens: str = "trade_boundaries",
    claim: str = "Byggström saknas i kalkylen.",
    cites: list[tuple[str, str]] | None = None,
    claim_type: str = "trade_boundary",
    **kw: Any,
) -> Flag:
    cites = cites or [("aaa111-000004", "Byggström tillhandahålls av beställaren")]
    kw.setdefault("money_basis", basis(item="Byggström per etapp"))
    return Flag(
        lens_id=lens,
        claim_type=claim_type,
        claim=claim,
        citations=[Citation(block_id=b, quote=q) for b, q in cites],
        **kw,
    )


def search(query: str, lens: str = "trade_boundaries", **kw: Any) -> ToolCall:
    return ToolCall(role="lens", lens_id=lens, turn=1, tool="search_blocks", input={"query": query}, **kw)


def lens_result(lens: str, flags: list[Flag], generic: bool = False, **kw: Any) -> LensResult:
    kw.setdefault("status", "submitted")
    return LensResult(lens_id=lens, model="claude-opus-5-5", flags=flags, generic=generic, **kw)
