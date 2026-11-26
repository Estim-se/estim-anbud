"""Export Estim's default catalog from the app repo into `~/estim-anbud-data/catalogs/estim-default.json`.

    uv run python -m anbud.granskning.export_catalog --hourly-rate <SEK per hour>

Reads the app's example catalog source read-only, applies its value updates, and writes a `Catalog` JSON (the format
in `anbud.granskning.catalog`) labelled as Estim's default. Nothing it reads is written into this repo. The app's own
default hourly rate is unset, so the rate is a required argument, and the label calls it assumed. The app's list
prices are material only, so they become `unit_price`; list discount and markup are left out (the app's defaults are
0).
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from anbud.granskning.catalog import DEFAULT_CATALOG, Catalog, CatalogItem, catalog_path

APP_SOURCE = Path.home() / "Documents/estim/workspace/estim/src/lib/tender-example-catalog.ts"
_ROW = re.compile(r"\[(\d+),\s*'((?:[^'\\]|\\.)*)',\s*([\d.]+),\s*([\d.]+),\s*(\d+)\]")
_SECTION = re.compile(r"(\d+):\s*'((?:[^'\\]|\\.)*)'")
_UPDATE = re.compile(
    r"id:\s*id\((\d+)\),\s*from:\s*\{[^}]*\},\s*to:\s*\{\s*hours:\s*([\d.]+),\s*list_price:\s*([\d.]+)\s*\}"
)


def _id(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def parse_catalog_source(source: str) -> list[CatalogItem]:
    """Every example post type, last definition per id winning, with the value updates applied."""
    sections: dict[str, str] = {}
    if "TENDER_SECTIONS" in source:
        sections = dict(_SECTION.findall(source[source.index("TENDER_SECTIONS") :].split("};")[0]))
    rows: dict[int, CatalogItem] = {}
    for n, name, hours, price, section in _ROW.findall(source):
        rows[int(n)] = CatalogItem(
            id=_id(int(n)),
            name=name.replace("\\'", "'"),
            hours=float(hours),
            unit_price=float(price),
            category=sections.get(section, section),
        )
    for n, hours, price in _UPDATE.findall(source):
        if int(n) in rows:
            rows[int(n)] = rows[int(n)].model_copy(update={"hours": float(hours), "unit_price": float(price)})
    unit = re.compile(r"\((m|st)\)\s*$")
    out = []
    for item in rows.values():
        match = unit.search(item.name)
        out.append(item.model_copy(update={"unit": match.group(1)}) if match else item)
    return out


def export(source: Path, out: Path, hourly_rate: float) -> Catalog:
    items = parse_catalog_source(source.read_text(encoding="utf-8"))
    if not items:
        raise ValueError("no catalog rows found in the source")
    catalog = Catalog(
        name=DEFAULT_CATALOG,
        label=f"Estims standardkatalog, antaget timpris {hourly_rate:g} kr/h, ej kundens",
        hourly_rate=hourly_rate,
        items=items,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(catalog.model_dump(mode="json"), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return catalog


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, default=APP_SOURCE)
    parser.add_argument("--hourly-rate", type=float, required=True, help="SEK per hour; the app's default is unset")
    args = parser.parse_args(argv)
    out = catalog_path(DEFAULT_CATALOG)
    catalog = export(args.source, out, args.hourly_rate)
    print(f"wrote {len(catalog.items)} items to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
