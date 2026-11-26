"""The calc as an XLSX with Swedish labels: Översikt, Kalkyl, Underlag, Objektmodell, Ej omhändertaget and Bortvalt med
skäl (lines set aside with a kind other than unclear, sorted by kind).

- A post whose count per object comes from a recipe says which recipe ("kundens recept" or Estim's default) in its
  notes and level, and lists the object rows it counts.

- Översikt says what the object list read by code counted, and for which kinds the model's evidence disagrees with it;
  Objektmodell says per kind whether code or the model counted it, and lists the blocks the model cited outside the
  list.
- Every sheet starts with the catalog's label, so a calc priced on Estim's default catalog says so on every page.
- Every post shows its citation (block id and quote). Tender text and model output are always written as plain
  strings, never as formulas, and characters a worksheet can't hold are removed.
- Amounts are values computed in Decimal by `price`, not formulas, so the file matches `posts.json` exactly.
"""

from __future__ import annotations

import io
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.worksheet import Worksheet

from anbud.granskning.catalog import Catalog
from anbud.kalkyl.price import OBJECT_LABELS, SCOPE_LABELS, Line, Post, assumed_posts
from anbud.kalkyl.records import SET_ASIDE_LABELS, Uncovered
from anbud.selection import excluded_counts, excluded_label

if TYPE_CHECKING:
    from anbud.granskning.corpus import Corpus
    from anbud.kalkyl.pipeline import KalkylResult

KR = '#,##0.00 "kr"'
NUM = "#,##0.##"
HOURS = '#,##0.00 "h"'
SHARE = "0%"
BOLD = Font(bold=True)
TITLE = Font(bold=True, size=14)
HEAD_FILL = PatternFill("solid", fgColor="D9E2EC")
WARN_FILL = PatternFill("solid", fgColor="FCE4D6")
FIRST_ROW = 5  # rows 1-3: label, status, run; row 4: column headings


def _put(ws: Worksheet, row: int, col: int, value: Any, fmt: str | None = None) -> None:
    cell = ws.cell(row=row, column=col)
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, str):
        value = ILLEGAL_CHARACTERS_RE.sub("", value)
        cell.value = value
        cell.data_type = "s"  # never a formula, whatever the text starts with
    else:
        cell.value = value
    if fmt:
        cell.number_format = fmt


def _header(ws: Worksheet, catalog: Catalog, status: str, run: str, columns: list[tuple[str, int]]) -> None:
    _put(ws, 1, 1, f"Katalog: {catalog.label}")
    ws.cell(row=1, column=1).font = BOLD
    _put(ws, 2, 1, status)
    if "ofullständig" in status.lower():
        ws.cell(row=2, column=1).fill = WARN_FILL
    _put(ws, 3, 1, run)
    for col, (name, width) in enumerate(columns, 1):
        _put(ws, 4, col, name)
        cell = ws.cell(row=4, column=col)
        cell.font, cell.fill = BOLD, HEAD_FILL
        ws.column_dimensions[cell.column_letter].width = width
    ws.freeze_panes = ws.cell(row=FIRST_ROW, column=1)


def _status(result: KalkylResult) -> str:
    if result.complete:
        return "Skuggkalkyl (Kalkyl v3) – komplett körning. Granskas av kalkylator före användning."
    return "Skuggkalkyl (Kalkyl v3) – OFULLSTÄNDIG: delar av underlaget lästes inte, se Ej omhändertaget."


def _citation(post: Post, lines: list[Line] | None = None) -> tuple[str, str]:
    cites = [c for ln in (lines or post.lines) for c in ln.record.record.citations]
    first = cites[0]
    more = f" (+{len(cites) - 1} källor i Underlag)" if len(cites) > 1 else ""
    return first.block_id, f"{first.quote}{more}"


CALC_COLUMNS = [
    ("Nr", 6),
    ("Post-id", 14),
    ("Post", 40),
    ("Typkod", 10),
    ("Omfattning", 14),
    ("Katalogpost", 30),
    ("Kategori", 16),
    ("Mängd", 10),
    ("Enhet", 8),
    ("À-pris", 12),
    ("Material", 14),
    ("Tim/enhet", 10),
    ("Timmar", 10),
    ("Arbete", 14),
    ("Summa", 14),
    ("Status", 14),
    ("Nivå", 14),
    ("Underlag för mängd", 40),
    ("Anmärkning", 40),
    ("Källa (block)", 18),
    ("Citat", 60),
    ("Räknade objektrader (block)", 40),
    ("Serieeffekt (timfaktor)", 12),
    ("Installerat pris per enhet", 14),
]
MAX_ROW_IDS = 400  # block ids listed in one cell; beyond that the cell gives the count and Objektmodell has the rest


def _rows_cell(post: Post) -> str:
    rows = post.rows
    if not rows:
        return ""
    if len(rows) > MAX_ROW_IDS:
        return f"{len(rows)} rader, se Objektmodell"
    return f"{len(rows)} rader: {', '.join(rows)}"


def _calc_sheet(ws: Worksheet, result: KalkylResult, catalog: Catalog, status: str, run: str) -> dict[str, int]:
    _header(ws, catalog, status, run, CALC_COLUMNS)
    rows: dict[str, int] = {}
    r = FIRST_ROW
    for n, p in enumerate(result.posts, 1):
        block, quote = _citation(p)
        notes = list(p.notes)
        if p.mapping_note:
            notes.append(f"Katalog: {p.mapping_note}")
        elif p.mapped_by == "R3":
            notes.append("Katalogpost vald av R3")
        elif p.mapped_by == "recept":
            notes.append("Katalogpost enligt receptet")
        if p.scope == "unclear":
            notes.append("Låg: oklar entreprenadgräns")
        values: list[tuple[Any, str | None]] = [
            (n, None),
            (p.id, None),
            (p.name, None),
            (p.type_code or "", None),
            (SCOPE_LABELS[p.scope], None),
            (f"{p.catalog_id} {p.catalog_name}".strip() if p.catalog_id else "", None),
            (p.category, None),
            (p.quantity, NUM),
            (p.unit, None),
            (p.unit_price, KR),
            (p.material, KR),
            (p.hours_per_unit, NUM),
            (p.hours, HOURS),
            (p.labour, KR),
            (p.total, KR),
            (p.status, None),
            (p.level, None),
            (p.basis, None),
            ("; ".join(notes), None),
            (block, None),
            (quote, None),
            (_rows_cell(p), None),
            (p.repetition_factor, NUM),
            (p.installed_unit_price, KR),
        ]
        for col, (value, fmt) in enumerate(values, 1):
            if value is not None:
                _put(ws, r, col, value, fmt)
        if p.status in ("Saknar pris", "Saknar mängd"):
            ws.cell(row=r, column=16).fill = WARN_FILL
        if p.assumed:
            ws.cell(row=r, column=17).fill = WARN_FILL
        rows[p.id] = r
        r += 1
    if result.posts:  # filter on Nivå, Status or Omfattning; the totals below stay outside the filter
        ws.auto_filter.ref = f"A{FIRST_ROW - 1}:{ws.cell(row=r - 1, column=len(CALC_COLUMNS)).coordinate}"
    t = result.totals
    r += 1
    for label, value, fmt in (
        ("Material", t.material, KR),
        ("Timmar", t.hours, HOURS),
        (
            f"varav avdrag för serieeffekt ({result.repetition.label if result.repetition else 'ingen tabell'})",
            -t.repetition_hours,
            HOURS,
        ),
        ("Arbete", t.labour, KR),
        ("Summa (exkl. optioner)", t.total, KR),
        ("varav oklar entreprenadgräns", t.unclear_total, KR),
        (f"varav antagna mängder ({t.assumed_priced} poster, filtrera Nivå = Antagande)", t.assumed_total, KR),
        ("Optioner (utanför summan)", t.options_total, KR),
    ):
        _put(ws, r, 3, label)
        ws.cell(row=r, column=3).font = BOLD
        _put(ws, r, 15, value, fmt)
        r += 1
    return rows


OVERVIEW_COLUMNS = [("Uppgift", 44), ("Värde", 24), ("Underlag", 50), ("Källa (block)", 18), ("Citat", 60)]
TOP_ASSUMED = 3  # the assumed posts Översikt names, largest first
OTHER_DISCIPLINE = "Ej lästa (annan disciplin)"
BOUNDARY_LINES = "Rader i andra discipliners handlingar som rör elarbetena (lästa)"


def _pct(part: Decimal, whole: Decimal) -> str:
    return f"{(part / whole * 100).quantize(Decimal(1))} %"


def _assumed_rows(result: KalkylResult) -> list[tuple[Any, ...]]:
    """How much of the total rests on assumed counts, and the largest assumed posts with the citation of the line
    the assumption sits on, so the estimator sees at once that the figure is an assumption and what to check."""
    t = result.totals
    share = t.assumed_total / t.total if t.total else "ingen summa"
    rows: list[tuple[Any, ...]] = [
        ("varav antagna mängder", t.assumed_total, KR),
        ("Andel av summan som vilar på antagna mängder", share, SHARE, "filtrera Nivå = Antagande i Kalkyl"),
        ("Prissatta poster med antagen mängd", t.assumed_priced, None),
    ]
    posts = assumed_posts(result.posts)
    for n, p in enumerate(posts[:TOP_ASSUMED], 1):
        lines = p.assumed_lines
        basis = "; ".join(dict.fromkeys(ln.basis for ln in lines))
        block, quote = _citation(p, lines)
        how = f"{_pct(p.total or Decimal(0), t.total)} av summan; {basis}; {p.id}"
        rows.append((f"Antagen post att kontrollera {n}: {p.name}", p.total, KR, how, block, quote))
    rest = posts[TOP_ASSUMED:]
    if rest:
        rows.append((f"Övriga antagna poster ({len(rest)})", sum((p.total or 0 for p in rest), Decimal(0)), KR))
    return rows


def _overview(
    ws: Worksheet, result: KalkylResult, catalog: Catalog, status: str, run: str, meta: dict, corpus: Corpus
) -> None:
    _header(ws, catalog, status, run, OVERVIEW_COLUMNS)
    _put(ws, FIRST_ROW, 1, "Kalkyl (skugga)")
    ws.cell(row=FIRST_ROW, column=1).font = TITLE
    t, c = result.totals, result.counts()
    rows: list[tuple[Any, ...]] = [
        ("Företag", meta["company_id"], None),
        ("Anbud", meta["tender_id"], None),
        ("Körning", meta["run_id"], None),
        ("Katalog", f"{catalog.name} ({catalog.currency})", None),
        ("Timpris", catalog.hourly_rate, KR),
        ("Recept", result.recipe.label if result.recipe else "inget recept: rumsposter utan antal saknar mängd", None),
        ("Summa (exkl. optioner)", t.total, KR),
        *_assumed_rows(result),
        ("Material", t.material, KR),
        ("Arbete", t.labour, KR),
        ("Timmar", t.hours, HOURS),
        ("Avdrag för serieeffekt (timmar)", -t.repetition_hours, HOURS),
        (
            "Serieeffekt",
            result.repetition.label if result.repetition else "ingen tabell: timmar per enhet är fasta",
            None,
        ),
        ("varav oklar entreprenadgräns", t.unclear_total, KR),
        ("Optioner (utanför summan)", t.options_total, KR),
        ("Poster", c["posts"], None),
        ("Prissatta poster", c["posts_priced"], None),
        ("Saknar pris", c["posts_missing_price"], None),
        ("Saknar mängd", c["posts_missing_quantity"], None),
        ("Mängdrader i underlaget", c["quantity_lines"], None),
        ("Omfattningsstycken i underlaget", c["scope_lines"], None),
        ("Rader som säger att något inte ingår", c["not_included_lines"], None),
        ("Bortval som annan entreprenör utan stöd i raden (till kontroll)", c["other_trade_unchecked"], None),
        ("Anslutningsposter för annans utrustning (tillagda av kod)", c["connection_posts"], None),
        ("Rader och stycken utan post", c["lines_not_handled"], None),
        ("varav ej omhändertagna (se Ej omhändertaget)", c["lines_unresolved"], None),
        (OTHER_DISCIPLINE, excluded_label(corpus), None, "sökbara; räknas inte i Ej omhändertaget"),
        (BOUNDARY_LINES, excluded_counts(corpus)["boundary_lines"], None),
        ("varav bortvalda med skäl (se Bortvalt med skäl)", c["lines_not_handled"] - c["lines_unresolved"], None),
        ("varav omfattningsstycken", c["scope_lines_not_handled"], None),
        ("Objekt lästa av kod ur objektlistan (som appen)", _code_objects(result), None),
        ("Modellen avviker från objektlistan för", _disagree(result), None),
        ("Poster per objekt eller rumstyp", c["posts_per_object"], None),
        ("Poster med antal enligt recept", c["posts_from_recipe"], None),
        ("Poster med antaget antal per objekt (1)", c["posts_assumed_per_object"], None),
        ("Poster med antaget antal objekt (minst 1 byggnad)", c["posts_assumed_object_count"], None),
        ("Verifierade poster från modellen", c["records_verified"], None),
        ("Underkända poster från modellen", c["records_rejected"], None),
        ("Avsnitt lästa / totalt", f"{c['chunk_status'].get('submitted', 0)} / {c['chunks']}", None),
        ("Modellkostnad (USD)", meta.get("cost_usd", 0.0), "0.0000"),
    ]
    for i, (label, value, fmt, *more) in enumerate(rows, FIRST_ROW + 2):
        _put(ws, i, 1, label)
        _put(ws, i, 2, value, fmt)
        for col, extra in enumerate(more, 3):
            _put(ws, i, col, extra)
        if label.startswith("Andel av summan") and t.assumed_total:
            ws.cell(row=i, column=2).fill = WARN_FILL


CODE_KINDS = ("apartment", "premises", "stairwell", "stairwell_floor", "building")
OBJECT_SOURCES = {"code": "kod (objektlistan)", "model": "modellen", "assumed": "antagande (minst 1)"}


def _code_objects(result: KalkylResult) -> str:
    if not result.object_lists:
        return "ingen objektlista hittades: antalen kommer från modellens rader"
    parts = []
    for kind in CODE_KINDS:
        obj = result.objects.get(kind)
        if obj is not None and obj.source == "code" and obj.count is not None:
            parts.append(f"{format(obj.count.normalize(), 'f')} {OBJECT_LABELS[kind][1]}")
    found = len(result.object_lists)
    return ", ".join(parts) + (f" ({found} listor hittades, den största används)" if found > 1 else "")


def _disagree(result: KalkylResult) -> str:
    kinds = result.code_model_disagree()
    if not result.object_lists:
        return "-"
    return ", ".join(OBJECT_LABELS[k][1] for k in kinds) if kinds else "ingen avvikelse"


def _evidence_sheet(ws: Worksheet, result: KalkylResult, catalog: Catalog, status: str, run: str) -> None:
    columns = [
        ("Post-id", 14),
        ("Rad-id", 14),
        ("Typ", 12),
        ("Vad", 40),
        ("Typkod", 10),
        ("Mängdkälla", 12),
        ("Mängd", 10),
        ("Per objekt", 12),
        ("Enhet", 8),
        ("Plats", 18),
        ("Omfattning", 14),
        ("Tilläggs", 8),
        ("Nivå", 14),
        ("Källa (block)", 18),
        ("Citat", 60),
    ]
    _header(ws, catalog, status, run, columns)
    post_of = {ln.record.id: p.id for p in result.posts for ln in p.lines}
    levels = {"exact": "Exakt citat", "visual": "Läst visuellt", "interpretation": "Tolkning"}
    r = FIRST_ROW
    for v in result.verified:
        rec = v.record
        per = OBJECT_LABELS[rec.per_object][0] if rec.per_object else ""
        kind = "Objekt" if rec.record_kind == "object" else "Krav"
        if rec.object_kind:
            kind = f"Objekt: {OBJECT_LABELS[rec.object_kind][0]}"
        for i, cite in enumerate(rec.citations):
            values = [
                post_of.get(v.id, ""),
                v.id,
                kind,
                rec.what,
                rec.type_code or "",
                rec.quantity_source,
                rec.quantity,
                per,
                rec.unit,
                rec.location or "",
                SCOPE_LABELS[rec.scope],
                "ja" if rec.additive else "",
                levels[v.level],
                cite.block_id,
                cite.quote,
            ]
            for col, value in enumerate(values, 1):
                if value is not None and (i == 0 or col >= 14 or col <= 2):
                    _put(ws, r, col, value, NUM if col == 7 else None)
            r += 1


def _objects_sheet(ws: Worksheet, result: KalkylResult, catalog: Catalog, status: str, run: str) -> None:
    columns = [
        ("Objekt", 16),
        ("Antal", 10),
        ("Underlag", 50),
        ("Konflikt", 40),
        ("Anmärkning", 40),
        ("Rader", 40),
        ("Räknat av", 14),
        ("Modellens källor utanför objektlistan", 40),
    ]
    _header(ws, catalog, status, run, columns)
    for r, obj in enumerate(result.objects.values(), FIRST_ROW):
        _put(ws, r, 1, OBJECT_LABELS[obj.kind][1])
        _put(ws, r, 2, obj.count, NUM)
        _put(ws, r, 3, obj.basis)
        _put(ws, r, 4, obj.conflict or "")
        _put(ws, r, 5, obj.note or "")
        _put(ws, r, 6, ", ".join(obj.rows))
        _put(ws, r, 7, OBJECT_SOURCES[obj.source])
        _put(ws, r, 8, ", ".join(obj.model_rows))


def _uncovered_sheet(
    ws: Worksheet,
    result: KalkylResult,
    lines: list[Uncovered],
    corpus: Corpus,
    catalog: Catalog,
    status: str,
    run: str,
) -> None:
    columns = [
        ("Källa (block)", 18),
        ("Dokument", 14),
        ("Sida/blad", 16),
        ("Rad", 6),
        ("Rubrik", 40),
        ("Text", 70),
        ("Orsak", 60),
        ("Typ", 16),
        ("Kategori", 30),
    ]
    _header(ws, catalog, status, run, columns)
    for r, u in enumerate(lines, FIRST_ROW):
        b = corpus.blocks[u.block_id]
        where = f"sida {b.page}" if b.page is not None else (b.sheet or "")
        kind = {"scope": "Omfattningsstycke", "not_included": "Rad om något som inte ingår"}.get(u.line, "Mängdrad")
        heading = " > ".join(result.paths.get(u.block_id, ()))
        category = SET_ASIDE_LABELS[u.set_aside_kind] if u.set_aside_kind else ""
        values = [u.block_id, b.doc_id, where, b.row, heading, b.text, u.reason, kind, category]
        for col, value in enumerate(values, 1):
            if value is not None:
                _put(ws, r, col, value)
        ws.cell(row=r, column=6).alignment = Alignment(wrap_text=True, vertical="top")


def write_workbook(result: KalkylResult, corpus: Corpus, catalog: Catalog, meta: dict) -> tuple[bytes, dict[str, int]]:
    """The XLSX bytes and each post's row on the Kalkyl sheet (for CalcPost.row)."""
    status = _status(result)
    run = f"Företag {meta['company_id']}, anbud {meta['tender_id']}, körning {meta['run_id']}"
    wb = Workbook()
    overview = wb.active
    overview.title = "Översikt"
    _overview(overview, result, catalog, status, run, meta, corpus)
    rows = _calc_sheet(wb.create_sheet("Kalkyl"), result, catalog, status, run)
    _evidence_sheet(wb.create_sheet("Underlag"), result, catalog, status, run)
    _objects_sheet(wb.create_sheet("Objektmodell"), result, catalog, status, run)
    unresolved = [u for u in result.uncovered if u.unresolved]
    reasoned = sorted((u for u in result.uncovered if not u.unresolved), key=lambda u: u.set_aside_kind or "")
    _uncovered_sheet(wb.create_sheet("Ej omhändertaget"), result, unresolved, corpus, catalog, status, run)
    _uncovered_sheet(wb.create_sheet("Bortvalt med skäl"), result, reasoned, corpus, catalog, status, run)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), rows
