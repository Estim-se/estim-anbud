"""Reports (plan §6.1 step 14): an HTML page and an XLSX workbook, written into the tender's data folder, never the
repo.

- HTML: every string from a model or a document is HTML-escaped and shown as plain text, never as markdown. The page
  has no scripts and a content security policy that forbids them, so a missed escape still can't run anything.
- XLSX: Swedish labels. Strings are always stored as text, never formulas. Columns are `Column`s with a stable `key`;
  a hidden sheet `_nycklar` maps each sheet's columns to their keys, so label import can find columns by key. Blind
  review passes its own `columns` (leaving out verdict and score) and `extra_columns` for the reviewer to fill in.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet
from pydantic import BaseModel, ConfigDict, Field

from anbud.granskning.catalog import Catalog
from anbud.granskning.checks import ChecksReport
from anbud.granskning.checks_report import add_check_sheets, checks_coverage_html, checks_html
from anbud.granskning.corpus import Corpus
from anbud.granskning.items import Item
from anbud.granskning.rank import Ranked, TenderCoverage, document_names
from anbud.models import Block
from anbud.paths import dev_tender_dir, repo_root

RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

VERDICTS = {"confirmed": "Bekräftad", "partly": "Delvis", "refuted": "Motbevisad"}
VERIFICATION = {
    "verified": "Verifierad",
    "sample": "Stickprov under gränsen",
    "failed": "Verifieringen gav inget svar",
    "not_selected": "Ej verifierad",
}
PLACEMENTS = {
    "shown": "Visas",
    "shown_unpriced": "Visas, utan prisuppgift",
    "generic": "Generisk",
    "minor": "Mindre (under väsentlighetsgränsen)",
    "refuted": "Motbevisad av verifieringen",
    "covered": "Täcks redan av kalkylen",
    "capped": "Utanför taket för sin lista",
    "zero": "Noll i poäng",
}
STATUSES = {
    "submitted": "Klar",
    "truncated": "Avbruten vid gränsen",
    "no_submit": "Inga fynd inlämnade",
    "error": "Fel",
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Report(_Model):
    run_id: str
    company_id: str
    tender_id: str
    created_at: datetime
    catalog: str | None = Field(default=None, description="The catalog's label, e.g. Estims standardkatalog")
    default_catalog: bool = False
    materiality_sek: float = 20000.0
    max_shown: int = 15
    max_unpriced_shown: int = 10
    shown: list[Item] = Field(default_factory=list)
    unpriced: list[Item] = Field(default_factory=list, description="Krav utan prisuppgift: not refuted, not priced")
    generic: list[Item] = Field(default_factory=list)
    logged: list[Item] = Field(default_factory=list)
    coverage: TenderCoverage
    sources: dict[str, str] = Field(default_factory=dict, description="block id -> where it is, for citations")
    cost_usd: float | None = None
    stop: str | None = Field(default=None, description="The run's hard stop: its size tier and the prefix it came from")
    checks: ChecksReport | None = Field(default=None, description="Zero rows, per-space rules, outlets per room type")


OTHER_DISCIPLINE = "Ej lästa (annan disciplin)"
SEARCH_ONLY = "Endast sökbara (prefixbudget)"
SEARCH_ONLY_P1 = "Prioritet 1 granskad via sökning, inte i sin helhet"
PARTIAL = "Partiell granskning: AF eller kontrakt via sökning"
STOP = "Kostnadsstopp (nivå efter storlek)"


def _where(block: Block, names: dict[str, str]) -> str:
    parts = [names.get(block.doc_id, block.doc_id)]
    if block.page is not None:
        parts.append(f"s. {block.page}")
    if block.sheet is not None:
        parts.append(f"blad {block.sheet}")
    if block.row is not None:
        parts.append(f"rad {block.row}")
    return ", ".join(parts)


def build_report(
    *,
    run_id: str,
    company_id: str,
    tender_id: str,
    ranked: Ranked,
    coverage: TenderCoverage,
    corpus: Corpus,
    catalog: Catalog | None,
    materiality_sek: float,
    max_shown: int = 15,
    max_unpriced_shown: int = 10,
    cost_usd: float | None = None,
    created_at: datetime | None = None,
) -> Report:
    names = document_names(corpus)
    sources = {}
    for item in ranked.shown + ranked.unpriced + ranked.generic + ranked.logged:
        for c in item.flag.citations:
            block = corpus.get(c.block_id)
            sources[c.block_id] = _where(block, names) if block else c.block_id
    return Report(
        run_id=run_id,
        company_id=company_id,
        tender_id=tender_id,
        created_at=created_at or datetime.now(UTC),
        catalog=catalog.label if catalog else None,
        default_catalog=bool(catalog and catalog.default),
        materiality_sek=materiality_sek,
        max_shown=max_shown,
        max_unpriced_shown=max_unpriced_shown,
        shown=ranked.shown,
        unpriced=ranked.unpriced,
        generic=ranked.generic,
        logged=ranked.logged,
        coverage=coverage,
        sources=sources,
        cost_usd=cost_usd,
    )


# ---------------------------------------------------------------- shared text


def sek(value: float | None) -> str:
    if value is None:
        return ""
    return f"{round(value):,}".replace(",", " ") + " kr"


def kronor_text(item: Item) -> str:
    k = item.kronor
    if k is None or not k.priced:
        return "Ej prissatt" + (f" ({k.reason})" if k and k.reason else "")
    return f"{sek(k.low)} – {sek(k.high)}" if k.low != k.high else sek(k.point)


def not_refuted(report: Report) -> list[Item]:
    """Every flag the verifier didn't refute, lists first, in report order: nothing is hidden from the XLSX."""
    items = report.shown + report.unpriced + report.generic + report.logged
    return [i for i in items if i.placement != "refuted"]


def verdict_text(item: Item) -> str:
    if item.verdict is None:
        return VERIFICATION[item.verification]
    text = VERDICTS[item.verdict.verdict]
    return f"{text} (stickprov)" if item.verification == "sample" else text


def _yes(value: bool | None) -> str:
    return "" if value is None else ("Ja" if value else "Nej")


# ---------------------------------------------------------------- HTML

_CSS = """
:root { color-scheme: light dark; --fg: #1d1d1f; --bg: #fff; --muted: #666; --line: #ddd; --tag: #f1efe8; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #eee; --bg: #18181a; --muted: #aaa; --line: #333; --tag: #2a2a2d; }
}
body { font: 15px/1.5 system-ui, sans-serif; color: var(--fg); background: var(--bg); margin: 0 auto; max-width: 960px;
  padding: 24px 16px; }
h1 { font-size: 22px; } h2 { font-size: 18px; margin-top: 36px; border-bottom: 1px solid var(--line); }
.meta, .muted { color: var(--muted); font-size: 13px; }
.flag { border: 1px solid var(--line); border-radius: 8px; padding: 12px 16px; margin: 12px 0; }
.flag h3 { font-size: 16px; margin: 0 0 6px; }
.tag { display: inline-block; background: var(--tag); border-radius: 4px; padding: 0 6px; margin-right: 6px;
  font-size: 12px; }
blockquote { margin: 6px 0; padding-left: 10px; border-left: 3px solid var(--line); white-space: pre-wrap; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
td, th { border-bottom: 1px solid var(--line); padding: 4px 6px; text-align: left; vertical-align: top; }
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _flag_html(n: int, item: Item, report: Report) -> str:
    f = item.flag
    tags = [verdict_text(item), ", ".join(item.lenses), f.claim_type]
    if f.contractual_exposure:
        tags.append("Avtalsrisk")
    if item.kronor and item.kronor.default_catalog:
        tags.append("Estims standardkatalog")
    out = [
        '<div class="flag">',
        f"<h3>{n}. {_e(f.claim)}</h3>",
        "<div>" + "".join(f'<span class="tag">{_e(t)}</span>' for t in tags) + "</div>",
        f"<p><strong>{_e(kronor_text(item))}</strong>"
        + (f' <span class="muted">{_e(item.kronor.label)}</span>' if item.kronor and item.kronor.priced else "")
        + "</p>",
    ]
    for c in f.citations:
        out.append(f'<div class="muted">{_e(report.sources.get(c.block_id, c.block_id))} · {_e(c.block_id)}</div>')
        out.append(f"<blockquote>{_e(c.quote)}</blockquote>")
    if f.absence:
        out.append(
            f'<p class="muted">Saknas, mätt mot: {_e(f.reference_rule)}. Sökningar: {_e("; ".join(f.searches))}</p>'
        )
    v = item.verdict
    if v is not None:
        details = [f"Elentreprenaden: {_yes(v.electrical_scope)}", f"Täcks av kalkylen: {_yes(v.covered_by_calc)}"]
        if v.calc_post:
            details.append(f"Kalkylpost: {v.calc_post}")
        out.append(f'<p class="muted">{_e(" · ".join(details))}</p>')
        out.append(f'<p class="muted">Fel om: {_e(v.would_be_wrong_if)}</p>')
    out.append(f'<p class="muted">Id {_e(item.fingerprint[:12])}</p></div>')
    return "\n".join(out)


def render_html(report: Report) -> str:
    cov = report.coverage
    parts = [
        "<!doctype html>",
        '<html lang="sv"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">",
        f"<title>Granskning {_e(report.tender_id)}</title><style>{_CSS}</style></head><body>",
        f"<h1>Granskning av anbud {_e(report.tender_id)}</h1>",
        f'<p class="meta">Körning {_e(report.run_id)} · {_e(report.company_id)} · '
        f"{_e(report.created_at.strftime('%Y-%m-%d %H:%M'))} UTC</p>",
    ]
    if report.stop:
        parts.append(f'<p class="meta">{STOP}: {_e(report.stop)}.</p>')
    if report.catalog:
        note = " Priserna är Estims standardvärden, inte företagets egna." if report.default_catalog else ""
        parts.append(f'<p class="meta">Kronor beräknade med {_e(report.catalog)}.{_e(note)}</p>')
    parts.append(
        f'<p class="meta">Högst {report.max_shown} prissatta flaggor och {report.max_unpriced_shown} krav utan '
        f"prisuppgift visas. Under {_e(sek(report.materiality_sek))} räknas en flagga som mindre, om den inte har en "
        "avtalsrisk.</p>"
    )
    parts.append(f"<h2>Flaggor ({len(report.shown)})</h2>")
    parts += [_flag_html(n, item, report) for n, item in enumerate(report.shown, 1)] or ["<p>Inga flaggor visas.</p>"]
    if report.unpriced:
        parts.append(f"<h2>Krav utan prisuppgift ({len(report.unpriced)})</h2>")
        parts.append(
            f'<p class="muted">Fynd som inte motbevisats men som inte kunde prissättas. Högst '
            f"{report.max_unpriced_shown} visas, ordnade efter verifieringens utfall och sedan allvar.</p>"
        )
        parts += [_flag_html(n, item, report) for n, item in enumerate(report.unpriced, 1)]
    if report.generic:
        parts.append(f"<h2>Generiska fynd ({len(report.generic)})</h2>")
        parts.append('<p class="muted">Normal omfattning som handlingarna inte nämner. Redovisas separat.</p>')
        parts += [_flag_html(n, item, report) for n, item in enumerate(report.generic, 1)]
    capped = [i for i in report.logged if i.placement == "capped"]
    if capped:
        parts.append(
            f'<p class="meta">{len(capped)} flaggor ryms inte i listorna ovan. Alla flaggor som inte motbevisats finns '
            "på fliken Alla flaggor i XLSX-filen, med var de hamnade och varför.</p>"
        )
    parts += checks_html(report.checks)
    parts.append("<h2>Täckning</h2>")
    parts.append(f"<p><strong>{_e(cov.note)}</strong></p>")
    parts.append(f"<p>{cov.documents} dokument i underlaget.</p>")
    if cov.not_read_by_prep:
        parts.append(f"<p>Kunde inte läsas: {_e(', '.join(cov.not_read_by_prep))}</p>")
    if cov.unread_tiles:
        unread = ", ".join(cov.unread_tiles)
        parts.append(f"<p>{len(cov.unread_tiles)} ritningsrutor kunde inte läsas av kortsteget: {_e(unread)}</p>")
    if cov.named_not_present:
        parts.append(f"<p>Nämns men finns inte i underlaget: {_e(', '.join(cov.named_not_present))}</p>")
    if cov.search_only_documents:
        parts.append(
            f"<p>{SEARCH_ONLY}: {len(cov.search_only_documents)} dokument. Prefixet höll bara deras rubriker; "
            f"granskningen nådde texten genom sökning. {_e(', '.join(cov.search_only_documents))}</p>"
        )
    if cov.search_only_priority_1:
        whole = "" if cov.search_only_core else "AF och kontrakt låg i sin helhet; d"
        parts.append(
            f"<p><strong>{SEARCH_ONLY_P1}</strong>: {len(cov.search_only_priority_1)} dokument. {whole or 'D'}essa "
            "prioritet 1-handlingar var för stora för prefixet och lästes genom sökning, så granskningen av dem är "
            f"ofullständig. {_e(', '.join(cov.search_only_priority_1))}</p>"
        )
    if cov.search_only_core:
        parts.append(
            f"<p><strong>{PARTIAL}</strong>: {len(cov.search_only_core)} dokument. Inte ens AF och kontrakt fick plats "
            "i sin helhet i prefixet, så hela granskningen är partiell: villkor som bara står i dem kan ha missats. "
            f"{_e(', '.join(cov.search_only_core))}</p>"
        )
    if cov.other_discipline:
        parts.append(
            f"<p>{OTHER_DISCIPLINE}: {_e(cov.other_discipline)}. Sökbara för hänvisningar; "
            f"{cov.boundary_lines} rader som rör elarbetena lästes ändå. "
            f"{_e(', '.join(cov.other_discipline_documents))}</p>"
        )
    for lens in cov.lenses:
        parts.append('<div class="flag">')
        parts.append(
            f"<h3>{_e(lens.lens_id)}{' (generisk)' if lens.generic else ''} · {_e(STATUSES.get(lens.status))}</h3>"
        )
        parts.append(
            f'<p class="muted">{lens.turns} varv · {lens.blocks_read} block lästa · {len(lens.searches)} sökningar · '
            f"{lens.tiles_viewed} ritningsrutor · {lens.calc_calls} kalkyluppslag · {lens.law_lookups} lagrum · "
            f"{lens.catalog_searches} katalogsökningar</p>"
        )
        if lens.documents_opened:
            parts.append(f"<p>Öppnade: {_e(', '.join(lens.documents_opened))}</p>")
        if lens.sheets_opened:
            parts.append(f"<p>Blad: {_e(', '.join(lens.sheets_opened))}</p>")
        if lens.searches:
            parts.append(f'<p class="muted">Sökningar: {_e("; ".join(lens.searches))}</p>')
        if lens.not_checked:
            parts.append("<p>Inte kontrollerat:</p><ul>")
            parts += [f"<li>{_e(line)}</li>" for line in lens.not_checked]
            parts.append("</ul>")
        parts.append("</div>")
    parts += checks_coverage_html(report.checks)
    if report.logged:
        parts.append(f"<h2>Loggade, visas inte ({len(report.logged)})</h2><table>")
        parts.append("<tr><th>Id</th><th>Varför</th><th>Påstående</th><th>Kronor</th><th>Verifiering</th></tr>")
        for item in report.logged:
            parts.append(
                f"<tr><td>{_e(item.fingerprint[:12])}</td><td>{_e(PLACEMENTS.get(item.placement or '', ''))}</td>"
                f"<td>{_e(item.flag.claim)}</td><td>{_e(kronor_text(item))}</td><td>{_e(verdict_text(item))}</td></tr>"
            )
        parts.append("</table>")
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------- XLSX


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    width: int = 14
    value: Callable[[Item, Report], Any] | None = None  # None: left empty, for a reviewer to fill in


def _quotes(item: Item, report: Report) -> str:
    return "\n".join(f"[{report.sources.get(c.block_id, c.block_id)}] {c.quote}" for c in item.flag.citations)


def _basis(item: Item) -> str:
    k = item.kronor
    if k is None:
        return ""
    return k.label if k.priced else (k.reason or "")


FLAG_COLUMNS: tuple[Column, ...] = (
    Column("fingerprint", "Fingeravtryck", 18, lambda i, r: i.fingerprint),
    Column("lenses", "Lins", 14, lambda i, r: ", ".join(i.lenses)),
    Column("claim_type", "Typ", 16, lambda i, r: i.flag.claim_type),
    Column("claim", "Påstående", 60, lambda i, r: i.flag.claim),
    Column("citations", "Citat och källa", 70, _quotes),
    Column("absence", "Saknas i underlaget", 10, lambda i, r: _yes(i.flag.absence)),
    Column("reference_rule", "Mätt mot", 30, lambda i, r: i.flag.reference_rule or ""),
    Column("kronor_low", "Kronor låg", 12, lambda i, r: i.kronor.low if i.kronor and i.kronor.priced else None),
    Column("kronor_high", "Kronor hög", 12, lambda i, r: i.kronor.high if i.kronor and i.kronor.priced else None),
    Column("kronor_basis", "Prisgrund", 40, lambda i, r: _basis(i)),
    Column(
        "default_catalog", "Estims standardkatalog", 10, lambda i, r: _yes(bool(i.kronor and i.kronor.default_catalog))
    ),
    Column("contractual_exposure", "Avtalsrisk", 10, lambda i, r: _yes(i.flag.contractual_exposure)),
    Column("minor", "Mindre", 10, lambda i, r: _yes(i.minor)),
    Column("verification", "Verifiering", 18, lambda i, r: VERIFICATION[i.verification]),
    Column("verdict", "Utlåtande", 14, lambda i, r: VERDICTS[i.verdict.verdict] if i.verdict else ""),
    Column(
        "electrical_scope", "Elentreprenaden", 10, lambda i, r: _yes(i.verdict.electrical_scope) if i.verdict else ""
    ),
    Column(
        "covered_by_calc", "Täcks av kalkylen", 10, lambda i, r: _yes(i.verdict.covered_by_calc) if i.verdict else ""
    ),
    Column("calc_post", "Kalkylpost", 16, lambda i, r: (i.verdict.calc_post or "") if i.verdict else ""),
    Column("would_be_wrong_if", "Fel om", 40, lambda i, r: i.verdict.would_be_wrong_if if i.verdict else ""),
    Column("score", "Poäng", 12, lambda i, r: round(i.score)),
    Column("placement", "Placering", 18, lambda i, r: PLACEMENTS.get(i.placement or "", "")),
)

COVERAGE_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("lens_id", "Lins", 16),
    ("status", "Status", 18),
    ("turns", "Varv", 8),
    ("blocks_read", "Block lästa", 10),
    ("searches", "Sökningar", 50),
    ("documents_opened", "Öppnade dokument", 50),
    ("sheets_opened", "Blad", 30),
    ("tiles_viewed", "Ritningsrutor", 10),
    ("calc_calls", "Kalkyluppslag", 10),
    ("law_lookups", "Lagrum", 8),
    ("catalog_searches", "Katalogsökningar", 10),
    ("not_checked", "Inte kontrollerat", 70),
)


def _cell_value(value: Any) -> Any:
    if isinstance(value, str):
        return ILLEGAL_CHARACTERS_RE.sub("", value)
    return value


def _put(ws: Worksheet, row: int, col: int, value: Any) -> None:
    cell = ws.cell(row=row, column=col, value=_cell_value(value))
    if isinstance(value, str):
        cell.data_type = "s"  # never a formula, whatever the text starts with
        cell.alignment = Alignment(wrap_text=True, vertical="top")


def _header(ws: Worksheet, labels: Sequence[tuple[str, int]]) -> None:
    for col, (label, width) in enumerate(labels, 1):
        _put(ws, 1, col, label)
        ws.cell(row=1, column=col).font = Font(bold=True)
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = "A2"
    if labels:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(labels))}1"


def _flag_sheet(wb: Workbook, title: str, items: list[Item], report: Report, columns: Sequence[Column]) -> Worksheet:
    ws = wb.create_sheet(title)
    _header(ws, [(c.label, c.width) for c in columns])
    for row, item in enumerate(items, 2):
        for col, column in enumerate(columns, 1):
            if column.value is not None:
                _put(ws, row, col, column.value(item, report))
    return ws


def build_workbook(
    report: Report, columns: Sequence[Column] = FLAG_COLUMNS, extra_columns: Sequence[Column] = ()
) -> Workbook:
    wb = Workbook()
    wb.remove(wb.active)
    cols = [*columns, *extra_columns]
    keys: list[tuple[str, list[str]]] = []
    sheets = (
        ("Flaggor", report.shown, cols),
        ("Krav utan prisuppgift", report.unpriced, cols),
        ("Generiska", report.generic, cols),
        ("Loggade", report.logged, cols),
        ("Alla flaggor", not_refuted(report), list(columns)),  # an index, not a labelling sheet: no review columns
    )
    for title, items, sheet_cols in sheets:
        _flag_sheet(wb, title, items, report, sheet_cols)
        keys.append((title, [c.key for c in sheet_cols]))
    ws = wb.create_sheet("Täckning")
    _header(ws, [(label, width) for _, label, width in COVERAGE_COLUMNS])
    for row, lens in enumerate(report.coverage.lenses, 2):
        for col, (key, _, _) in enumerate(COVERAGE_COLUMNS, 1):
            value = getattr(lens, key)
            if key == "status":
                value = STATUSES.get(value, value)
            elif isinstance(value, list):
                value = "\n".join(value) if key == "not_checked" else "; ".join(value)
            _put(ws, row, col, value)
    keys.append(("Täckning", [k for k, _, _ in COVERAGE_COLUMNS]))
    keys += add_check_sheets(wb, report.checks)
    about = wb.create_sheet("Om rapporten")
    rows = [
        ("Anbud", report.tender_id),
        ("Företag", report.company_id),
        ("Körning", report.run_id),
        ("Skapad (UTC)", report.created_at.strftime("%Y-%m-%d %H:%M")),
        ("Katalog", report.catalog or "Ingen"),
        ("Estims standardkatalog", _yes(report.default_catalog)),
        ("Väsentlighetsgräns", sek(report.materiality_sek)),
        ("Dokument i underlaget", report.coverage.documents),
        (OTHER_DISCIPLINE, report.coverage.other_discipline or "0 sidor, 0 dokument"),
        (SEARCH_ONLY, len(report.coverage.search_only_documents)),
        *(  # only when some were: a report where P1 fits keeps its rows
            [(SEARCH_ONLY_P1, ", ".join(report.coverage.search_only_priority_1))]
            if report.coverage.search_only_priority_1
            else []
        ),
        *([(PARTIAL, ", ".join(report.coverage.search_only_core))] if report.coverage.search_only_core else []),
        ("Modellkostnad (USD)", round(report.cost_usd, 2) if report.cost_usd is not None else "Okänd"),
        (STOP, report.stop or "Okänt"),
        ("Täckning", report.coverage.note),
    ]
    for row, (label, value) in enumerate(rows, 1):
        _put(about, row, 1, label)
        _put(about, row, 2, value)
    about.column_dimensions["A"].width = 24
    about.column_dimensions["B"].width = 80
    meta = wb.create_sheet("_nycklar")
    meta.sheet_state = "hidden"
    _put(meta, 1, 1, "run_id")
    _put(meta, 1, 2, report.run_id)
    for row, (title, sheet_keys) in enumerate(keys, 2):
        _put(meta, row, 1, title)
        for col, key in enumerate(sheet_keys, 2):
            _put(meta, row, col, key)
    return wb


def column_keys(wb: Workbook, sheet: str) -> dict[str, int]:
    """key -> 1-based column number on `sheet`, from the hidden `_nycklar` sheet."""
    meta = wb["_nycklar"]
    for row in meta.iter_rows(min_row=2, values_only=True):
        if row and row[0] == sheet:
            return {key: col for col, key in enumerate(row[1:], 1) if key}
    raise KeyError(sheet)


# ---------------------------------------------------------------- writing


def tender_run_dir(company_id: str, tender_id: str, run_id: str, root: Path | None = None) -> Path:
    """`<data>/<company>/dev/<tender>/runs/<run_id>`: a run's outputs, next to the tender they review."""
    if not RUN_ID.fullmatch(run_id):
        raise ValueError("run ids are letters, digits, dot, dash and underscore")
    return dev_tender_dir(company_id, tender_id, root) / "runs" / run_id


def report_dir(company_id: str, tender_id: str, run_id: str, root: Path | None = None) -> Path:
    return tender_run_dir(company_id, tender_id, run_id, root) / "report"


def write_reports(report: Report, out_dir: Path, extra_columns: Sequence[Column] = ()) -> dict[str, Path]:
    """Writes granskning.html and granskning.xlsx into `out_dir`, which must be outside the repo."""
    target = out_dir.expanduser().resolve()
    repo = repo_root().resolve()
    if target == repo or repo in target.parents:
        raise ValueError("reports are written into the tender's data folder, never the repo")
    target.mkdir(parents=True, exist_ok=True)
    html_path = target / "granskning.html"
    html_path.write_text(render_html(report), encoding="utf-8")
    xlsx_path = target / "granskning.xlsx"
    build_workbook(report, extra_columns=extra_columns).save(xlsx_path)
    return {"html": html_path, "xlsx": xlsx_path}
