"""The checks' report sections: HTML fragments and XLSX sheets for zero rows, per-space rules, outlets per room type,
and the checks' coverage. Every string is escaped (HTML) or stored as text (XLSX), like the rest of the report.

Wording rule for outlets per room type: the reference is the customer's recipe ("kundens recept anger N"); a standard
is only named by the code and clause the cited FU block writes, never as the source of a number.
"""

from __future__ import annotations

import html
from collections.abc import Callable
from typing import Any

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from anbud.granskning.checks import ChecksReport
from anbud.granskning.zero_rows import NOT_FOUND

ZERO_STATUS = {"explained": "Förklarad", "contradicted": "Motsägs", "not_found": "Ingen hänvisning hittad"}
RULE_STATUS = {"gap": "Kalkylen har för lite", "ok": "Stämmer", "no_post": "Ingen post i kalkylen"}
RECIPE_STATUS = {
    "no_trigger": "Ingen hänvisning till en standard för uttag per rumstyp hittades.",
    "no_recipe": "Kundens recept finns inte inlagt. Jämför kalkylens recept per rum för hand.",
    "compared": "Jämfört med kundens recept.",
    "failed": "Kontrollen gav inget användbart svar.",
}


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _n(value: float | None) -> str:
    return "" if value is None else f"{value:g}"


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    out = ["<table>", "<tr>" + "".join(f"<th>{_e(h)}</th>" for h in headers) + "</tr>"]
    out += ["<tr>" + "".join(f"<td>{_e(c)}</td>" for c in row) + "</tr>" for row in rows]
    out.append("</table>")
    return out


def _zero_rows(checks: ChecksReport) -> list[list[Any]]:
    return [
        [
            r.post_id,
            r.text,
            r.why,
            ZERO_STATUS[r.status],
            f"{r.where} · {r.block_id}" if r.block_id else NOT_FOUND,
            r.quote or "",
            r.note,
        ]
        for r in checks.zero_rows
    ]


ZERO_HEADERS = ["Post", "Kalkyltext", "Varför", "Status", "Källa", "Citat", "Kommentar"]


def _rule_rows(checks: ChecksReport) -> list[list[Any]]:
    return [
        [
            r.item,
            _n(r.per_space),
            ", ".join(r.space_types),
            f"{r.rule_where} · {r.rule_block_id}",
            r.rule_quote,
            len(r.spaces),
            ", ".join(r.spaces),
            _n(r.required),
            r.post_id or "",
            _n(r.calc_quantity),
            _n(r.gap),
            RULE_STATUS[r.status],
        ]
        for r in checks.space_rules
    ]


RULE_HEADERS = [
    "Föremål",
    "Per utrymme",
    "Utrymmestyper",
    "Regel, källa",
    "Regel, citat",
    "Utrymmen",
    "Utrymmen, namn",
    "Krävs",
    "Kalkylpost",
    "Kalkylen har",
    "Fattas",
    "Status",
]


def _recipe_rows(checks: ChecksReport) -> list[list[Any]]:
    section = checks.recipe
    if section is None:
        return []
    return [
        [
            r.room_type,
            r.item_kind,
            _n(r.calc_count),
            ", ".join(r.posts),
            _n(r.recipe_count),
            _n(r.rooms),
            _n(r.gap_per_room),
            _n(r.gap_quantity),
        ]
        for r in section.rows
    ]


RECIPE_HEADERS = [
    "Rumstyp",
    "Typ",
    "Kalkylen har per rum",
    "Kalkylposter",
    "Kundens recept anger",
    "Antal rum",
    "Fattas per rum",
    "Fattas totalt",
]


def _recipe_lines(checks: ChecksReport) -> list[str]:
    section = checks.recipe
    if section is None:
        return []
    lines = [RECIPE_STATUS[section.status]]
    if section.trigger_block_id:
        ref = f" ({section.reference})" if section.reference else ""
        lines.append(f"FU hänvisar{ref}: {section.trigger_where} · {section.trigger_block_id}")
    if section.recipe_source:
        lines.append(f"Kundens recept: {section.recipe_source}")
    return lines


def checks_html(checks: ChecksReport | None) -> list[str]:
    """The checks' sections, for the page before coverage."""
    if checks is None:
        return []
    out = [f"<h2>Nollrader och ingår ej ({len(checks.zero_rows)})</h2>"]
    if checks.zero_rows:
        out += _table(ZERO_HEADERS, _zero_rows(checks))
    else:
        out.append("<p>Kalkylen har inga nollrader eller ingår ej-rader.</p>")
    out.append(f"<h2>Regler per utrymme ({len(checks.space_rules)})</h2>")
    if checks.space_rules:
        out += _table(RULE_HEADERS, _rule_rows(checks))
    else:
        out.append("<p>Inga regler per utrymme hittades i handlingarna.</p>")
    out.append("<h2>Uttag per rumstyp</h2>")
    out += [f"<p>{_e(line)}</p>" for line in _recipe_lines(checks)]
    if checks.recipe is not None and checks.recipe.trigger_quote:
        out.append(f"<blockquote>{_e(checks.recipe.trigger_quote)}</blockquote>")
    if _recipe_rows(checks):
        out += _table(RECIPE_HEADERS, _recipe_rows(checks))
    return out


def checks_coverage_html(checks: ChecksReport | None) -> list[str]:
    if checks is None:
        return []
    out = ["<h3>Kontroller</h3>"]
    for c in checks.coverage:
        out.append('<div class="flag">')
        out.append(f"<h3>{_e(c.title)} · {_e(c.status)}</h3>")
        out.append(f"<p>{_e(c.checked)}</p>")
        out.append(f'<p class="muted">{c.turns} varv · {c.blocks_read} block lästa · {len(c.searches)} sökningar</p>')
        if c.searches:
            out.append(f'<p class="muted">Sökningar: {_e("; ".join(c.searches))}</p>')
        if c.not_checked:
            out.append("<p>Inte kontrollerat:</p><ul>" + "".join(f"<li>{_e(n)}</li>" for n in c.not_checked) + "</ul>")
        out.append("</div>")
    return out


# XLSX


def _put(ws: Any, row: int, col: int, value: Any) -> None:
    cell = ws.cell(row=row, column=col, value=ILLEGAL_CHARACTERS_RE.sub("", value) if isinstance(value, str) else value)
    if isinstance(value, str):
        cell.data_type = "s"
        cell.alignment = Alignment(wrap_text=True, vertical="top")


def _sheet(wb: Workbook, title: str, headers: list[str], rows: list[list[Any]], width: int = 22) -> list[str]:
    ws = wb.create_sheet(title)
    for col, label in enumerate(headers, 1):
        _put(ws, 1, col, label)
        ws.cell(row=1, column=col).font = Font(bold=True)
        ws.column_dimensions[get_column_letter(col)].width = width
    for r, row in enumerate(rows, 2):
        for col, value in enumerate(row, 1):
            _put(ws, r, col, value)
    ws.freeze_panes = "A2"
    return headers


def _keys(headers: list[str]) -> list[str]:
    return [h.casefold().replace(", ", "_").replace(" ", "_") for h in headers]


def add_check_sheets(wb: Workbook, checks: ChecksReport | None) -> list[tuple[str, list[str]]]:
    """The checks' sheets; returns (sheet, column keys) for the hidden key sheet."""
    if checks is None:
        return []
    out = []
    sheets: list[tuple[str, list[str], Callable[[ChecksReport], list[list[Any]]]]] = [
        ("Nollrader och ingår ej", ZERO_HEADERS, _zero_rows),
        ("Regler per utrymme", RULE_HEADERS, _rule_rows),
        ("Uttag per rumstyp", RECIPE_HEADERS, _recipe_rows),
    ]
    for title, headers, rows in sheets:
        _sheet(wb, title, headers, rows(checks))
        out.append((title, _keys(headers)))
    recipe = wb["Uttag per rumstyp"]
    for n, line in enumerate(_recipe_lines(checks), recipe.max_row + 2):
        _put(recipe, n, 1, line)
    cov_headers = ["Kontroll", "Status", "Varv", "Kostnad USD", "Block lästa", "Sökningar", "Kontrollerat"]
    cov_headers.append("Inte kontrollerat")
    cov_rows = [
        [c.title, c.status, c.turns, c.cost_usd, c.blocks_read, "; ".join(c.searches), c.checked]
        + ["\n".join(c.not_checked)]
        for c in checks.coverage
    ]
    _sheet(wb, "Kontroller", cov_headers, cov_rows)
    out.append(("Kontroller", _keys(cov_headers)))
    return out
