"""Blind labels (plan §8.2): `anbud export-review` writes an XLSX for a human labeller, `anbud import-labels` reads it
back.

**Input.** A JSONL file of `ReviewFlag`s, one per flag the run produced. The report's final list maps onto it one to
one: the merged `Flag`, its fingerprint, whether it is `shown` (inside the cap) and the verifier's verdict if it has
one. Kronor, rank and lens reasoning are never read. Flags outside the cap, and refuted ones, may be sampled as decoys.

**Export.** One row per shown flag plus decoys, about `DECOY_SHARE` of the rows, in a shuffled order:
- a row shows an opaque number, the claim and its quotes with their place in the documents. No verdict, kronor, rank,
  lens, claim type or fingerprint is visible, and the sheet holds no hidden column;
- decoys are sampled flags (refuted or below the cap) and flags built by code to be plausible but wrong: a citation
  swapped for a neighbouring block, or another flag's claim on these citations;
- the decision column is a dropdown of `TAXONOMY`; minutes and a comment are optional. Everything else is locked.
The answer key (which row is which fingerprint, which rows are decoys) goes to a separate JSON file beside the XLSX.

**Import.** Checks the filled XLSX against its key, refusing the whole file on any error, then writes
`labels-<export>.jsonl` and `decoys-<export>.jsonl`, keyed by fingerprint, rewritten on each import of that export.
Decoy performance is reported separately from the flags'.

Everything is written to the tender's `labels/` folder in the data root, never the repo. Printed summaries carry counts
only; errors name row numbers, never cell text.
"""

from __future__ import annotations

import io
import json
import random
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import openpyxl
from openpyxl.styles import Alignment, Font, Protection
from openpyxl.worksheet.datavalidation import DataValidation
from pydantic import BaseModel, ConfigDict

from anbud.granskning.contracts import fingerprint
from anbud.granskning.corpus import Corpus
from anbud.models import Block, Citation, Flag
from anbud.store import RunStore, canonical

DECOY_SHARE = 0.2
SAMPLED_SHARE = 0.5  # at most this share of the decoys are sampled flags; code builds the rest
QUOTE_CHARS = 220
MAX_MINUTES = 600
EXPORT_ID = re.compile(r"^[0-9a-f]{8}$")
LABELLER = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

SHEET = "Granska"
INFO = "Instruktion"
TAXONOMY_SHEET = "Beslut"
HEADER = ("Nr", "Påstående", "Citat", "Beslut", "Minuter", "Kommentar")


@dataclass(frozen=True)
class Label:
    code: str
    text: str  # what the dropdown shows
    accepted: bool  # counts as an accepted catch for precision
    meaning: str


TAXONOMY = (
    Label("accept", "Rätt: väsentlig", True, "Stämmer, och det är värt att ta med eller fråga om i anbudet."),
    Label("accept_minor", "Rätt: liten", False, "Stämmer, men beloppet är under väsentlighetsgränsen."),
    Label("covered", "Rätt: redan i kalkylen", False, "Stämmer, men kalkylen täcker det redan."),
    Label("other_trade", "Rätt: annan entreprenör", False, "Stämmer, men kostnaden hör till en annan entreprenör."),
    Label("wrong_quote", "Fel: citatet stöder det inte", False, "Citatet finns inte eller säger inte detta."),
    Label("wrong_reading", "Fel: feltolkat", False, "Citatet stämmer, men påståendet tolkar det fel."),
    Label("practice_differs", "Praxis skiljer", False, "Beror på hur just ert företag brukar hantera det."),
    Label("unsure", "Osäker", False, "Går inte att avgöra utan mer underlag."),
)
_BY_TEXT = {lab.text.casefold(): lab for lab in TAXONOMY} | {lab.code: lab for lab in TAXONOMY}


class LabelError(RuntimeError):
    """Raised with reasons that name rows and columns, never cell text."""


class ReviewFlag(BaseModel):
    """One flag from a run, as the export takes it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    flag: Flag
    fingerprint: str | None = None  # checked against contracts.fingerprint when given
    shown: bool = True  # inside the cap; flags outside it may be sampled as decoys
    verdict: Literal["confirmed", "partly", "refuted"] | None = None


def read_review_flags(path: Path) -> list[ReviewFlag]:
    items = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = ReviewFlag.model_validate_json(line)
        except ValueError:
            raise LabelError(f"line {n} of the flags file isn't a review flag") from None
        if item.fingerprint is not None and item.fingerprint != fingerprint(item.flag):
            raise LabelError(f"line {n}: the fingerprint doesn't match the flag's citations and claim type")
        items.append(item)
    return items


# --- Building the review --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    item: str
    flag: Flag
    fingerprint: str
    decoy: str | None  # None for a real flag; else "refuted", "below_cap", "neighbour_swap" or "claim_swap"
    source: str | None  # the fingerprint a decoy was built or sampled from
    verdict: str | None


def _quote(block: Block) -> str:
    text = block.text
    if len(text) <= QUOTE_CHARS:
        return text
    cut = text.rfind(" ", 0, QUOTE_CHARS)
    return text[: cut if cut > QUOTE_CHARS // 2 else QUOTE_CHARS]


def _neighbour_swap(flag: Flag, corpus: Corpus, rng: random.Random) -> Flag | None:
    """The flag with one citation moved to a nearby block in the same document: same claim, wrong evidence."""
    cited = {c.block_id for c in flag.citations}
    order = list(range(len(flag.citations)))
    rng.shuffle(order)
    for i in order:
        near = [
            b
            for b in corpus.neighbours(flag.citations[i].block_id, n=2)
            if b.id not in cited and b.kind != "heading" and len(b.text) >= 20
        ]
        if near:
            pick = rng.choice(near)
            citations = list(flag.citations)
            citations[i] = Citation(block_id=pick.id, quote=_quote(pick))
            return flag.model_copy(update={"citations": citations})
    return None


def build_review(items: list[ReviewFlag], corpus: Corpus, export_id: str) -> list[Row]:
    """The shown flags plus decoys, shuffled. Deterministic for a given export id."""
    rng = random.Random(f"anbud-review-{export_id}")
    shown = [i for i in items if i.shown]
    if not shown:
        raise LabelError("no shown flags to review")
    real = {fingerprint(i.flag) for i in shown}
    rows = [Row("", i.flag, fingerprint(i.flag), None, None, i.verdict) for i in shown]
    wanted = max(1, round(len(shown) * DECOY_SHARE / (1 - DECOY_SHARE)))
    taken: set[str] = set(real)

    pool = [i for i in items if not i.shown and fingerprint(i.flag) not in taken]
    rng.shuffle(pool)
    for item in pool[: int(wanted * SAMPLED_SHARE)]:
        fp = fingerprint(item.flag)
        kind = "refuted" if item.verdict == "refuted" else "below_cap"
        rows.append(Row("", item.flag, fp, kind, fp, item.verdict))
        taken.add(fp)

    sources = list(shown)
    rng.shuffle(sources)
    attempts = 0
    while len(rows) - len(shown) < wanted and attempts < 4 * wanted:
        source = sources[attempts % len(sources)].flag
        attempts += 1
        decoy, kind = _neighbour_swap(source, corpus, rng), "neighbour_swap"
        if decoy is None and len(sources) > 1:
            other = rng.choice([s.flag for s in sources if s.flag.claim != source.claim] or [source])
            # Another flag's claim (and claim type, so the fingerprint differs) on these citations.
            decoy = source.model_copy(update={"claim": other.claim, "claim_type": other.claim_type})
            decoy, kind = (decoy if other.claim != source.claim else None), "claim_swap"
        if decoy is None:
            continue
        fp = fingerprint(decoy)
        if fp not in taken:
            rows.append(Row("", decoy, fp, kind, fingerprint(source), None))
            taken.add(fp)
    rng.shuffle(rows)
    return [Row(f"{n:03d}", r.flag, r.fingerprint, r.decoy, r.source, r.verdict) for n, r in enumerate(rows, 1)]


def _place(block: Block | None, corpus: Corpus) -> str:
    if block is None:
        return "okänt block"
    doc = next((d for d in corpus.documents if d["doc_id"] == block.doc_id), None)
    name = Path(doc["paths"][0]).name if doc and doc.get("paths") else block.doc_id
    if block.sheet is not None:
        return f"{name}, blad {block.sheet}, rad {block.row}"
    return f"{name}, sida {block.page}" if block.page is not None else name


def _citations_text(flag: Flag, corpus: Corpus) -> str:
    return "\n\n".join(f"{_place(corpus.get(c.block_id), corpus)}:\n»{c.quote}«" for c in flag.citations)


# --- The workbook ---------------------------------------------------------------------------------------------------


def review_workbook(rows: list[Row], corpus: Corpus, export_id: str) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = SHEET
    ws.append(HEADER)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in rows:
        ws.append([row.item, row.flag.claim, _citations_text(row.flag, corpus), None, None, None])
    unlocked = Protection(locked=False)
    wrap = Alignment(wrap_text=True, vertical="top")
    for r in range(2, len(rows) + 2):
        for c in range(1, len(HEADER) + 1):
            cell = ws.cell(r, c)
            cell.alignment = wrap
            if c >= 4:
                cell.protection = unlocked
    for col, width in zip("ABCDEF", (6, 60, 80, 28, 10, 40), strict=True):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    last = len(rows) + 1
    choice = DataValidation(
        type="list",
        formula1=f"='{TAXONOMY_SHEET}'!$A$2:$A${len(TAXONOMY) + 1}",
        allow_blank=True,
        showErrorMessage=True,
        errorTitle="Ogiltigt beslut",
        error="Välj ett beslut i listan.",
    )
    minutes = DataValidation(
        type="decimal",
        operator="between",
        formula1="0",
        formula2=str(MAX_MINUTES),
        allow_blank=True,
        showErrorMessage=True,
        error=f"Minuter mellan 0 och {MAX_MINUTES}.",
    )
    ws.add_data_validation(choice)
    ws.add_data_validation(minutes)
    choice.add(f"D2:D{last}")
    minutes.add(f"E2:E{last}")
    ws.protection.sheet = True
    ws.protection.formatColumns = False
    ws.protection.formatRows = False

    tax = wb.create_sheet(TAXONOMY_SHEET)
    tax.append(("Beslut", "Betydelse"))
    for lab in TAXONOMY:
        tax.append((lab.text, lab.meaning))
    tax.column_dimensions["A"].width = 30
    tax.column_dimensions["B"].width = 70
    tax.protection.sheet = True

    info = wb.create_sheet(INFO, 0)
    info.append(("Export", export_id))
    for line in (
        "Gå igenom varje rad på bladet Granska och välj ett beslut i listan.",
        "Läs påståendet och citaten. Stöds påståendet av citaten, och spelar det roll för anbudet?",
        "Raderna står i slumpad ordning. Några rader är kontrollrader; behandla dem som alla andra.",
        "Fyll gärna i hur många minuter raden tog, och en kort kommentar.",
        "Ändra inte numren och ta inte bort rader.",
    ):
        info.append(("", line))
    info.column_dimensions["B"].width = 100
    info.protection.sheet = True

    wb.properties.creator = "Estim"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_review(
    company_id: str,
    tender_dir: Path,
    flags_file: Path,
    export_id: str | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    export_id = export_id or secrets.token_hex(4)
    if not EXPORT_ID.fullmatch(export_id):
        raise LabelError("export ids are 8 lowercase hex characters")
    corpus = Corpus.load(tender_dir / "prep")
    rows = build_review(read_review_flags(flags_file), corpus, export_id)
    store = RunStore(company_id, root)
    out = tender_dir / "labels"
    xlsx, key_path = out / f"review-{export_id}.xlsx", out / f"review-{export_id}.key.json"
    if xlsx.exists() or key_path.exists():
        raise LabelError("an export with this id already exists")
    key = {
        "export_id": export_id,
        "company_id": company_id,
        "rows": [
            {
                "item": r.item,
                "fingerprint": r.fingerprint,
                "decoy": r.decoy,
                "source": r.source,
                "lens_id": r.flag.lens_id,
                "claim_type": r.flag.claim_type,
                "verdict": r.verdict,
            }
            for r in rows
        ],
    }
    store.write_file(key_path, canonical(key))
    store.write_file(xlsx, review_workbook(rows, corpus, export_id))
    decoys = sum(r.decoy is not None for r in rows)
    return {"export_id": export_id, "rows": len(rows), "flags": len(rows) - decoys, "decoys": decoys, "xlsx": str(xlsx)}


# --- Import ---------------------------------------------------------------------------------------------------------


def _cell_text(value: object) -> str:
    return "" if value is None else str(value).strip()


def read_labels(data: bytes, key: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per row of the key: its label code (or None), minutes and comment. Raises with every error at once."""
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    except Exception:  # openpyxl raises many kinds on a damaged file
        raise LabelError("the file isn't a readable XLSX") from None
    if SHEET not in wb.sheetnames or INFO not in wb.sheetnames:
        raise LabelError("the workbook lacks its review or instruction sheet")
    if _cell_text(wb[INFO]["B1"].value) != key["export_id"]:
        raise LabelError("the workbook belongs to another export")
    ws = wb[SHEET]
    if tuple(_cell_text(c.value) for c in ws[1][: len(HEADER)]) != HEADER:
        raise LabelError("the review sheet's header was changed")
    rows = {r["item"]: r for r in key["rows"]}
    errors: list[str] = []
    seen: dict[str, dict[str, Any]] = {}
    for n, cells in enumerate(ws.iter_rows(min_row=2, max_col=len(HEADER), values_only=True), 2):
        if all(_cell_text(v) == "" for v in cells):
            continue
        item = _cell_text(cells[0])
        if item not in rows:
            errors.append(f"row {n}: unknown number")
            continue
        if item in seen:
            errors.append(f"row {n}: number repeated")
            continue
        decision, minutes_raw, comment = _cell_text(cells[3]), cells[4], _cell_text(cells[5])
        label = None
        if decision:
            lab = _BY_TEXT.get(decision.casefold())
            if lab is None:
                errors.append(f"row {n}: the decision isn't one in the list")
            label = lab
        minutes = None
        if _cell_text(minutes_raw):
            try:
                minutes = float(str(minutes_raw).replace(",", "."))
            except ValueError:
                minutes = -1.0
            if not 0 <= minutes <= MAX_MINUTES:
                errors.append(f"row {n}: minutes must be a number from 0 to {MAX_MINUTES}")
        seen[item] = {"item": item, "label": label, "minutes": minutes, "comment": comment or None}
    if missing := len(set(rows) - set(seen)):
        errors.append(f"{missing} row(s) of the export are missing")
    if errors:
        raise LabelError("; ".join(errors))
    return [seen[item] for item in rows]


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def import_labels(
    company_id: str, tender_dir: Path, xlsx: Path, labeller: str, root: Path | None = None
) -> dict[str, Any]:
    if not LABELLER.fullmatch(labeller):
        raise LabelError("labeller ids are neutral: lowercase letters, digits and dashes, like lab-1")
    data = xlsx.read_bytes()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True)
        export_id = _cell_text(wb[INFO]["B1"].value) if INFO in wb.sheetnames else ""
        wb.close()
    except Exception:
        raise LabelError("the file isn't a readable XLSX") from None
    if not EXPORT_ID.fullmatch(export_id):
        raise LabelError("the workbook has no export id")
    out = tender_dir / "labels"
    key_path = out / f"review-{export_id}.key.json"
    if not key_path.exists():
        raise LabelError("no export with this id for this tender")
    key = json.loads(key_path.read_text())
    if key.get("company_id") != company_id:
        raise LabelError("the export belongs to another company")
    entries = read_labels(data, key)
    by_item = {r["item"]: r for r in key["rows"]}
    flags, decoys = [], []
    for entry in entries:
        lab: Label | None = entry["label"]
        if lab is None:
            continue
        row = by_item[entry["item"]]
        record = {
            "fingerprint": row["fingerprint"],
            "label": lab.code,
            "accepted": lab.accepted,
            "minutes": entry["minutes"],
            "comment": entry["comment"],
            "labeller": labeller,
            "blind": True,
            "company_id": company_id,
            "export_id": export_id,
            "item": entry["item"],
            "lens_id": row["lens_id"],
            "claim_type": row["claim_type"],
        }
        if row["decoy"]:
            decoys.append(record | {"decoy": row["decoy"], "source": row["source"]})
        else:
            flags.append(record)
    store = RunStore(company_id, root)
    for name, records in ((f"labels-{export_id}.jsonl", flags), (f"decoys-{export_id}.jsonl", decoys)):
        store.write_file(out / name, b"".join(canonical(r) + b"\n" for r in records))

    def part(records: list[dict[str, Any]], total: int) -> dict[str, Any]:
        accepted = sum(r["accepted"] for r in records)
        by_label: dict[str, int] = {}
        for r in records:
            by_label[r["label"]] = by_label.get(r["label"], 0) + 1
        return {
            "rows": total,
            "labelled": len(records),
            "accepted": accepted,
            "accept_rate": _rate(accepted, len(records)),
            "by_label": dict(sorted(by_label.items())),
        }

    total_decoys = sum(1 for r in key["rows"] if r["decoy"])
    minutes = [e["minutes"] for e in entries if e["minutes"] is not None]
    return {
        "export_id": export_id,
        "flags": part(flags, len(key["rows"]) - total_decoys),
        "decoys": part(decoys, total_decoys),
        "minutes": round(sum(minutes), 1),
    }
