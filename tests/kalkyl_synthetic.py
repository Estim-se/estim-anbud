"""A synthetic prepared tender, a synthetic catalog and a scripted fake SDK for the Kalkyl tests.

Every text, id and price here is invented. Files go into temp folders only.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

from anbud.models import Block
from anbud.prep.model import Tile

DOC_A = "aaaaaaaaaaaa"
DOC_B = "bbbbbbbbbbbb"


def _b(n: int, doc: str, kind: str, text: str, **kw: Any) -> Block:
    return Block(id=f"{doc[:6]}-{n:06d}", doc_id=doc, kind=kind, text=text, **kw)  # type: ignore[arg-type]


BLOCKS = [
    _b(1, DOC_A, "heading", "1 Allmänt", page=1),
    _b(2, DOC_A, "paragraph", "Entreprenaden omfattar elarbeten i ett flerbostadshus med tre trapphus.", page=1),
    _b(3, DOC_A, "heading", "2 Installationer", page=1),
    _b(4, DOC_A, "paragraph", "Varje lägenhet förses med 2 st dubbla vägguttag i sovrum.", page=1),
    _b(5, DOC_A, "table_row", "L1 | Downlight | 24 st", page=2),
    _b(6, DOC_A, "table_row", "L2 | Armatur trapphus | 12 st", page=2),
    _b(7, DOC_A, "paragraph", "Kabelstege 45 m i källare.", page=2),
    _b(8, DOC_A, "paragraph", "Ny lägenhetscentral i varje lägenhet.", page=2),
    _b(9, DOC_A, "heading", "3 Option", page=3),
    _b(10, DOC_A, "paragraph", "Option: laddstolpe 4 st vid parkering.", page=3),
    _b(11, DOC_A, "paragraph", "Rörarbeten 30 m utförs av VVS-entreprenören.", page=3),
    _b(12, DOC_A, "paragraph", "Handlingen är daterad 2026-01-15.", page=3),
    _b(1, DOC_B, "sheet_row", "Lgh | Rum | Kök | Bad", sheet="Lägenheter", row=1),
    _b(2, DOC_B, "sheet_row", "1001 | 3 | 1 | 1", sheet="Lägenheter", row=2),
    _b(3, DOC_B, "sheet_row", "1002 | 2 | 1 | 1", sheet="Lägenheter", row=3),
    _b(4, DOC_B, "sheet_row", "1003 | 4 | 1 | 1", sheet="Lägenheter", row=4),
    _b(5, DOC_B, "sheet_row", "Summa | 9", sheet="Lägenheter", row=5),
]
OUTLINE = [
    {"doc_id": DOC_A, "level": 1, "title": "1 Allmänt", "number": "1", "block_id": "aaaaaa-000001", "page": 1},
    {"doc_id": DOC_A, "level": 1, "title": "2 Installationer", "number": "2", "block_id": "aaaaaa-000003", "page": 1},
    {"doc_id": DOC_A, "level": 1, "title": "3 Option", "number": "3", "block_id": "aaaaaa-000009", "page": 3},
    {"doc_id": DOC_B, "level": 1, "title": "Lägenheter", "block_id": "bbbbbb-000001", "sheet": "Lägenheter"},
]

CATALOG = {
    "name": "synthetic",
    "label": "Syntetisk testkatalog",
    "currency": "SEK",
    "hourly_rate": 650,
    "items": [
        {
            "id": "uttag-dubbelt",
            "name": "Vägguttag dubbelt",
            "unit": "st",
            "unit_price": 85.5,
            "hours": 0.45,
            "category": "Uttag",
        },
        {
            "id": "downlight",
            "name": "Downlight LED",
            "unit": "st",
            "unit_price": 420,
            "hours": 0.8,
            "category": "Belysning",
        },
        {
            "id": "armatur-trapphus",
            "name": "Armatur trapphus",
            "unit": "st",
            "unit_price": 900,
            "hours": 1,
            "category": "Belysning",
        },
        {
            "id": "kabelstege",
            "name": "Kabelstege",
            "unit": "m",
            "unit_price": 150,
            "hours": 0.3,
            "category": "Förläggning",
        },
        {
            "id": "laddstolpe",
            "name": "Laddstolpe",
            "unit": "st",
            "unit_price": 12000,
            "hours": 6,
            "category": "Laddning",
        },
        {
            "id": "lgh-central",
            "name": "Lägenhetscentral",
            "unit": "st",
            "unit_price": 2500,
            "hours": 3,
            "category": "Central",
        },
    ],
}
CARD_TEXT = "Planritning plan 2\n\nUttag 3 st i förråd"
PRICE_STRINGS = ["85.5", "85,5", "420", "12000", "2500", "650"]


def add_tile(prep_dir: Path, store: Any, png: bytes = b"synthetic tile image") -> Tile:
    """One tile on document A's page 3, its image in the company's blob store."""
    tile = Tile(
        id="aaaaaa-p3-t1",
        doc_id=DOC_A,
        page=3,
        index=1,
        reason="scan",
        dpi=200,
        x0=0,
        y0=0,
        x1=10,
        y1=10,
        blob=store.put_blob(png),
    )
    (prep_dir / "tiles.json").write_text(json.dumps([tile.model_dump()]))
    return tile


def write_prep(prep_dir: Path, blocks: list[Block] | None = None, outline: list[dict] | None = None) -> Path:
    prep_dir.mkdir(parents=True, exist_ok=True)
    (prep_dir / "blocks.jsonl").write_text("".join(b.model_dump_json() + "\n" for b in blocks or BLOCKS))
    (prep_dir / "outline.json").write_text(json.dumps(OUTLINE if outline is None else outline))
    (prep_dir / "tiles.json").write_text("[]")
    (prep_dir / "documents.json").write_text("[]")
    return prep_dir


def write_catalog(root: Path, name: str = "synthetic", catalog: dict | None = None) -> Path:
    path = root / "catalogs" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalog or CATALOG))
    return path


def cite(block_id: str, quote: str) -> dict[str, str]:
    return {"block_id": block_id, "quote": quote}


def req(what: str, src: str, unit: str, cites: list[dict], **kw: Any) -> dict[str, Any]:
    return {
        "record_kind": "requirement",
        "what": what,
        "quantity_source": src,
        "unit": unit,
        "scope": "included",
        "citations": cites,
        **kw,
    }


def obj(kind: str, src: str, cites: list[dict], **kw: Any) -> dict[str, Any]:
    return {
        "record_kind": "object",
        "what": kind,
        "object_kind": kind,
        "quantity_source": src,
        "unit": "st",
        "scope": "included",
        "citations": cites,
        **kw,
    }


# What a good R2 answer looks like for each chunk of the synthetic tender, keyed by its first block.
GOOD = {
    "aaaaaa-000001": {
        "records": [
            req(
                "Dubbla vägguttag i sovrum",
                "per_object",
                "st",
                [cite("aaaaaa-000004", "2 st dubbla vägguttag")],
                quantity=2,
                per_object="apartment",
                location="sovrum",
                catalog_id="uttag-dubbelt",
            ),
            req(
                "Downlight",
                "stated",
                "st",
                [cite("aaaaaa-000005", "L1 | Downlight | 24 st")],
                quantity=24,
                type_code="L1",
                catalog_id="downlight",
            ),
            req(
                "Armatur trapphus",
                "stated",
                "st",
                [cite("aaaaaa-000006", "Armatur trapphus | 12 st")],
                quantity=12,
                type_code="L2",
                catalog_id="armatur-trapphus",
            ),
            req(
                "Kabelstege",
                "stated",
                "m",
                [cite("aaaaaa-000007", "Kabelstege 45 m")],
                quantity=45,
                location="källare",
                catalog_id="kabelstege",
            ),
            req(
                "Lägenhetscentral",
                "per_object",
                "st",
                [cite("aaaaaa-000008", "lägenhetscentral i varje lägenhet")],
                quantity=1,
                per_object="apartment",
            ),
            req(
                "Laddstolpe",
                "stated",
                "st",
                [cite("aaaaaa-000010", "laddstolpe 4 st")],
                quantity=4,
                scope="option",
                catalog_id="laddstolpe",
            ),
            req(
                "Rörarbeten",
                "stated",
                "m",
                [cite("aaaaaa-000011", "Rörarbeten 30 m")],
                quantity=30,
                scope="other_trade",
            ),
        ],
        "set_aside": [],
    },
    "bbbbbb-000001": {
        "records": [
            obj("apartment", "list_row", [cite("bbbbbb-000002", "1001")]),
            obj("apartment", "list_row", [cite("bbbbbb-000003", "1002")]),
            obj("apartment", "list_row", [cite("bbbbbb-000004", "1003")]),
        ],
        "set_aside": [{"block_ids": ["bbbbbb-000005"], "reason": "summary row of the list"}],
    },
}


def usage(input_tokens: int = 1000, output_tokens: int = 200) -> Usage:
    return Usage(
        input_tokens=input_tokens, output_tokens=output_tokens, cache_creation_input_tokens=0, cache_read_input_tokens=0
    )


def message(content: list[Any], stop_reason: str = "tool_use", model: str = "claude-sonnet-5-5") -> Message:
    return Message(
        id="msg_synthetic",
        type="message",
        role="assistant",
        model=model,
        content=content,
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=usage(),
    )


def submit(payload: dict[str, Any], name: str = "submit_quantities", tool_id: str = "toolu_1") -> ToolUseBlock:
    return ToolUseBlock(type="tool_use", id=tool_id, name=name, input=payload)


def text(t: str) -> TextBlock:
    return TextBlock(type="text", text=t)


def first_block(params: dict[str, Any]) -> str | None:
    content = params["messages"][0]["content"]
    m = re.search(r'<block id="([^"]+)"(?![^>]*context="1")', content if isinstance(content, str) else "")
    return m.group(1) if m else None


class _Stream:
    def __init__(self, msg: Message) -> None:
        self.msg = msg
        self.request_id = "req_synthetic"

    def __enter__(self) -> _Stream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get_final_message(self) -> Message:
        return self.msg


class ScriptedMessages:
    """A fake `client.messages`: `handler(params)` returns the Message for each call. Records what was sent."""

    def __init__(self, handler: Callable[[dict[str, Any]], Message]) -> None:
        self.handler = handler
        self.sent: list[dict[str, Any]] = []

    def count_tokens(self, **params: Any) -> Any:
        return SimpleNamespace(input_tokens=1000)

    def stream(self, **params: Any) -> _Stream:
        self.sent.append(params)
        return _Stream(self.handler(params))

    def create(self, **params: Any) -> Any:
        raise AssertionError("ModelClient must stream")


def good_handler(params: dict[str, Any]) -> Message:
    """R1 writes a small card; R2 answers from GOOD; R3 answers none for everything."""
    if "tools" not in params:
        return message([text(CARD_TEXT)], stop_reason="end_turn")
    tool = params["tools"][0]["name"]
    if tool == "submit_matches":
        ids = re.findall(r'<post id="([^"]+)"', params["messages"][0]["content"])
        return message([submit({"matches": [{"post_id": i, "catalog_id": "none"} for i in ids]}, "submit_matches")])
    return message([submit(GOOD.get(first_block(params) or "", {"records": [], "set_aside": []}))])


def fake_sdk(handler: Callable[[dict[str, Any]], Message] = good_handler) -> SimpleNamespace:
    return SimpleNamespace(messages=ScriptedMessages(handler))
