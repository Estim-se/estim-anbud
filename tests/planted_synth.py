"""A synthetic dev tender for the planted-defect tests: Estim-written, invented, built in a temp folder.

Three documents (administrative terms, a scope description, a drawing) as prep output, and a typed calc.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from anbud.granskning.contracts import CalcPost
from anbud.models import Block
from anbud.prep.model import OutlineEntry
from anbud.prep.text import BlockIds, doc_id_of, heading_number

# (doc name, kind of document, [(block kind, text, heading level or None)])
DOCS = [
    (
        "af",
        "text",
        [
            ("heading", "AFC.1 Omfattning", 2),
            ("paragraph", "Entreprenaden omfattar elinstallationer i 24 lgh enligt rambeskrivningen.", None),
            ("heading", "AFC.22 Tider", 2),
            ("paragraph", "Entreprenaden ska vara färdigställd senast fyrtio veckor efter startbesked.", None),
            ("heading", "AFC.4 Kontroll", 2),
            ("paragraph", "Entreprenören upprättar en egen kontrollplan före start.", None),
            ("paragraph", "Kontrollplanen redovisas vid första byggmötet.", None),
            ("heading", "AFC.6 Ersättning", 2),
            ("paragraph", "Ersättning betalas enligt betalningsplan med månadsvisa delbetalningar.", None),
            ("heading", "AFC.7 Besiktning", 2),
            ("paragraph", "Inför slutbesiktning lämnas protokoll från kontroller enligt AFC.4.", None),
        ],
    ),
    (
        "rb",
        "text",
        [
            ("paragraph", "Totalt berörs 24 lgh i två trapphus.", None),
            ("paragraph", "Varje lägenhet får 6 uttag i kök och en strömbrytare per rum.", None),
            ("paragraph", "I varje trapphus monteras 12 armaturer med närvarostyrning.", None),
            ("paragraph", "I källare och trapphus utförs rivning av befintlig installation.", None),
            ("paragraph", "Elcentraler byts i samtliga lägenheter.", None),
        ],
    ),
    (
        "ritning",
        "pdf",
        [("tile_text", "Plan 1. Gruppcentral och stigarledning i trapphus A.", None)],
    ),
]


def _sha(name: str) -> str:
    return hashlib.sha256(f"synthetic-{name}".encode()).hexdigest()


def write_prep(prep_dir: Path) -> None:
    prep_dir.mkdir(parents=True, exist_ok=True)
    blocks: list[Block] = []
    outline: list[OutlineEntry] = []
    documents = []
    for name, kind, items in DOCS:
        sha = _sha(name)
        doc_id = doc_id_of(sha)
        ids = BlockIds(sha)
        pages = [{"page": 1, "width": 1190.0, "height": 842.0, "kind": "drawing", "chars": 0, "tiles": 1}]
        count = 0
        for n, (block_kind, text, level) in enumerate(items, 1):
            block = Block(id=ids.next(text), doc_id=doc_id, kind=block_kind, text=text, page=1, line=n)
            blocks.append(block)
            count += 1
            if level is not None:
                number, _ = heading_number(text)
                outline.append(
                    OutlineEntry(doc_id=doc_id, level=level, title=text, number=number, block_id=block.id, page=1)
                )
        documents.append(
            {
                "doc_id": doc_id,
                "sha256": sha,
                "kind": kind,
                "size": 1000,
                "paths": [f"{name}.{kind}"],
                "skipped": None,
                "pages": pages if kind == "pdf" else [],
                "sheets": [],
                "blocks": count,
                "tiles": 0,
                "masked": 0,
            }
        )
    (prep_dir / "blocks.jsonl").write_text("".join(b.model_dump_json() + "\n" for b in blocks))
    (prep_dir / "outline.json").write_text(json.dumps([o.model_dump() for o in outline]))
    (prep_dir / "documents.json").write_text(json.dumps(documents))
    (prep_dir / "tiles.json").write_text("[]")
    (prep_dir / "masking.jsonl").write_text("")
    (prep_dir / "summary.json").write_text(json.dumps({"documents": 3, "blocks_by_kind": {}}))


def _post(n: int, section: str | None, text: str, **kw: float | str) -> CalcPost:
    return CalcPost(id=f"src-{n}", sheet="Kalkyl", row=n + 1, section=section, text=text, **kw)  # type: ignore[arg-type]


def calc_posts() -> list[CalcPost]:
    rows = [
        ("Rivning", "Rivning av belysning i trapphus", dict(quantity=24, unit="st", unit_price=400, hours=12)),
        ("Rivning", "Rivning av installation i källare", dict(quantity=2, unit="st", unit_price=8000, hours=32)),
        ("Rivning", "Rivning i trapphus sker etappvis", {}),
        ("Installation", "Strömbrytare infälld", dict(quantity=120, unit="st", unit_price=150, hours=30)),
        ("Installation", "Dimmer", dict(quantity=24, unit="st", unit_price=450, hours=12)),
        ("Installation", "Vägguttag 2-vägs", dict(quantity=144, unit="st", unit_price=180, hours=48)),
        (
            "Installation",
            "Armatur trapphus med närvarostyrning",
            dict(quantity=24, unit="st", unit_price=900, hours=18),
        ),
        ("Installation", "Kabel EKK 3G1,5", dict(quantity=1200, unit="m", unit_price=12, hours=60)),
        ("Installation", "Gruppcentral inkl. jordfelsbrytare", dict(quantity=24, unit="st", unit_price=4500, hours=48)),
        ("Installation", "Brytare placeras enligt ritning", {}),
        ("Besiktning och dokumentation", "Egenkontroll och provning", dict(amount=12000, hours=24)),
        ("Besiktning och dokumentation", "Besiktning", dict(quantity=1, unit="st", unit_price=9000, hours=12)),
        (
            "Besiktning och dokumentation",
            "Relationshandlingar",
            dict(quantity=1, unit="st", unit_price=15000, hours=20),
        ),
        ("Projektledning", "Projektledning", dict(amount=60000, hours=80)),
        ("Projektledning", "Byggmöten", dict(quantity=10, unit="st", unit_price=1200, hours=20)),
        (None, "Etablering", dict(amount=20000)),
    ]
    return [_post(n, section, text, **kw) for n, (section, text, kw) in enumerate(rows, 1)]


def write_calc(path: Path) -> None:
    """The calc as a contractor might export it: its own column names, section rows and subtotal rows."""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Kalkyl"
    ws.append(["Pos", "Benämning", "Antal", "Enh", "À-pris", "Tim", "Summa"])
    section: str | None = None
    subtotal = 0.0
    for post in [p for p in calc_posts() if p.quantity is not None or p.amount is not None or p.hours is not None]:
        if post.section != section:
            if section is not None:
                ws.append([None, f"Summa {section}", None, None, None, None, subtotal])
            section, subtotal = post.section, 0.0
            ws.append([None, section or "Övrigt"])
        amount = post.amount if post.unit_price is None else (post.quantity or 0) * post.unit_price
        subtotal += amount or 0
        ws.append([None, post.text, post.quantity, post.unit, post.unit_price, post.hours, amount])
    ws.append([None, f"Summa {section}", None, None, None, None, subtotal])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def write_tender(tender_dir: Path) -> None:
    write_prep(tender_dir / "prep")
    write_calc(tender_dir / "calc" / "kalkyl.xlsx")
