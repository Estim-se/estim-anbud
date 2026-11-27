"""A synthetic tender with every trade's documents, in Estim's own words: electrical, common, other trades, and one
document nothing identifies. File names follow the common Swedish discipline-letter practice; no name is a real one."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from anbud.models import Block

# doc_id, relative path, pages, and the blocks: (kind, text, page)
DOCS: list[tuple[str, str, int, list[tuple[str, str, int]]]] = [
    (
        "e00000000001",
        "Förfrågningsunderlag/El/E-63-1-001 Teknisk beskrivning el.pdf",
        3,
        [
            ("heading", "1 Elinstallationer", 1),
            ("paragraph", "Varje lägenhet förses med 2 st dubbla vägguttag i sovrum.", 1),
            ("paragraph", "Kabelstege 45 m i källare.", 2),
        ],
    ),
    (
        "c00000000001",
        "Förfrågningsunderlag/AF.pdf",
        2,
        [
            ("heading", "AFC.1 Entreprenadform", 1),
            ("paragraph", "Entreprenaden utförs som underentreprenad.", 1),
        ],
    ),
    (
        "v00000000001",
        "Förfrågningsunderlag/VS/R-50-1-001 Teknisk beskrivning VS.pdf",
        40,
        [
            ("heading", "1 Rörinstallationer", 1),
            ("paragraph", "Rör 30 m isoleras med 50 mm isolering.", 1),
            ("heading", "2 Pumpar", 2),
            ("paragraph", "Cirkulationspumpar elansluts av elentreprenören.", 2),
            ("paragraph", "Pumparna monteras på konsol.", 2),
        ],
    ),
    (
        "k00000000001",
        "Förfrågningsunderlag/Konstruktion/K-20-1-001 Stomritning.pdf",
        25,
        [("paragraph", "Bjälklag 250 mm betong, 12 st håltagningar.", 1)],
    ),
    (
        "a00000000001",
        "Förfrågningsunderlag/A-40-1-001 Planritning.pdf",
        12,
        [("paragraph", "Plan 2, 4 st lägenheter.", 1)],
    ),
    (
        "u00000000001",
        "Förfrågningsunderlag/Bilaga 7.pdf",
        1,
        [("paragraph", "Uppgifter om befintliga förhållanden.", 1)],
    ),
]
EL, AF, VS, K, A, UNKNOWN = (d[0] for d in DOCS)


Doc = tuple[str, str, int, list[tuple[str, str, int]]]


def block_id(doc_id: str, n: int) -> str:
    return f"{doc_id[:6]}-{n:06d}"


def blocks(docs: list[Doc] | None = None) -> list[Block]:
    out = []
    for doc_id, _, _, items in docs or DOCS:
        for n, (kind, text, page) in enumerate(items, 1):
            out.append(Block(id=block_id(doc_id, n), doc_id=doc_id, kind=kind, text=text, page=page))  # type: ignore[arg-type]
    return out


def outline(docs: list[Doc] | None = None) -> list[dict[str, Any]]:
    return [
        {"doc_id": doc_id, "level": 1, "title": text, "block_id": block_id(doc_id, n), "page": page}
        for doc_id, _, _, items in docs or DOCS
        for n, (kind, text, page) in enumerate(items, 1)
        if kind == "heading"
    ]


def documents(docs: list[Doc] | None = None) -> list[dict[str, Any]]:
    return [
        {
            "doc_id": doc_id,
            "kind": "pdf",
            "paths": [path],
            "pages": [{"page": p, "width": 595, "height": 842, "kind": "text", "chars": 100} for p in range(1, n + 1)],
            "blocks": len(items),
        }
        for doc_id, path, n, items in docs or DOCS
    ]


def write_mixed_prep(prep_dir: Path, docs: list[Doc] | None = None) -> Path:
    """The tender's prep folder, with only `docs` if given."""
    prep_dir.mkdir(parents=True, exist_ok=True)
    (prep_dir / "blocks.jsonl").write_text("".join(b.model_dump_json() + "\n" for b in blocks(docs)))
    (prep_dir / "outline.json").write_text(json.dumps(outline(docs)))
    (prep_dir / "tiles.json").write_text("[]")
    (prep_dir / "documents.json").write_text(json.dumps(documents(docs)))
    return prep_dir
