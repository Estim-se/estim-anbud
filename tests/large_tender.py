"""A generated 1000-page tender with every trade's documents, in Estim's own filler words, sized like a full tender set
(about 1 650 prefix tokens per page). File names follow the common Swedish discipline-letter practice; none is real."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from anbud.models import Block

BLOCKS_PER_PAGE = 8
FILLER = "Text om utförande, material och mått i avsnitt {p}, rad {i}. " * 5  # about 300 characters, no trade words

# (path, pages, el-referring lines planted on its pages)
DOCS: list[tuple[str, int, int]] = [
    ("Förfrågningsunderlag/AF.pdf", 30, 0),
    ("Förfrågningsunderlag/Kontraktsförslag.pdf", 8, 0),
    ("Förfrågningsunderlag/Rambeskrivning.pdf", 20, 0),
    ("Förfrågningsunderlag/Tidplan.pdf", 2, 0),
    ("El/E-63-1-001 Teknisk beskrivning el.pdf", 60, 0),
    *((f"El/E-60-1-{n:03d} Planritning el.pdf", 12, 0) for n in range(1, 11)),
    ("El/Mängdförteckning el.pdf", 15, 0),
    ("Förfrågningsunderlag/Gränsdragningslista.pdf", 5, 0),
    ("Förfrågningsunderlag/Rumsbeskrivning.pdf", 30, 0),
    *((f"Förfrågningsunderlag/Bilaga {n}.pdf", 10, 0) for n in range(1, 6)),
    ("VS/R-50-1-001 Teknisk beskrivning VS.pdf", 120, 6),
    ("VS/R-52-1-001 Planritning VS.pdf", 80, 0),
    ("Ventilation/V-57-1-001 Teknisk beskrivning V.pdf", 100, 4),
    ("Ventilation/V-57-1-002 Planritning V.pdf", 50, 0),
    ("Konstruktion/K-20-1-001.pdf", 150, 0),
    ("Arkitekt/A-40-1-001.pdf", 120, 0),
    ("Mark/M-10-1-001.pdf", 40, 0),
]
TOTAL_PAGES = sum(pages for _, pages, _ in DOCS)
EL_LINE = "Pumpar och fläktar elansluts av elentreprenören."


def doc_id(n: int) -> str:
    return f"{n:02d}aaaaaaaaaa"


def build() -> tuple[list[Block], list[dict[str, Any]], list[dict[str, Any]]]:
    blocks: list[Block] = []
    outline: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    for n, (path, pages, el_lines) in enumerate(DOCS, 1):
        did = doc_id(n)
        k = 0
        for page in range(1, pages + 1):
            k += 1
            heading = Block(id=f"{did[:6]}-{k:06d}", doc_id=did, kind="heading", text=f"Avsnitt {page}", page=page)
            blocks.append(heading)
            outline.append({"doc_id": did, "level": 1, "title": heading.text, "block_id": heading.id, "page": page})
            for i in range(BLOCKS_PER_PAGE):
                k += 1
                text = EL_LINE if page <= el_lines and i == 0 else FILLER.format(p=page, i=i)
                blocks.append(Block(id=f"{did[:6]}-{k:06d}", doc_id=did, kind="paragraph", text=text, page=page))
        documents.append(
            {
                "doc_id": did,
                "kind": "pdf",
                "paths": [path],
                "pages": [
                    {"page": p, "width": 595, "height": 842, "kind": "text", "chars": 2400} for p in range(pages)
                ],
                "blocks": k,
            }
        )
    return blocks, outline, documents


def write_large_prep(prep_dir: Path) -> Path:
    blocks, outline, documents = build()
    prep_dir.mkdir(parents=True, exist_ok=True)
    (prep_dir / "blocks.jsonl").write_text("".join(b.model_dump_json() + "\n" for b in blocks))
    (prep_dir / "outline.json").write_text(json.dumps(outline))
    (prep_dir / "tiles.json").write_text("[]")
    (prep_dir / "documents.json").write_text(json.dumps(documents))
    return prep_dir
