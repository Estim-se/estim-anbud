"""Export Estim's default catalog from the app repo into `~/estim-anbud-data/catalogs/estim-default.json`.

    uv run python -m anbud.granskning.export_catalog --hourly-rate <SEK per hour>

Reads the app's example catalog source read-only, applies its value updates, and writes a `Catalog` JSON marked as
Estim's default. Nothing it reads is written into this repo. The app's own default hourly rate is 0, so the rate is an
argument; without one, kronor can price contract formulas and calc-post gaps but not hours or catalog items.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from anbud.granskning.kronor import Catalog, CatalogItem, catalog_path

APP_SOURCE = Path.home() / "Documents/estim/workspace/estim/src/lib/tender-example-catalog.ts"
_ROW = re.compile(r"\[(\d+),\s*'((?:[^'\\]|\\.)*)',\s*([\d.]+),\s*([\d.]+),\s*(\d+)\]")
_UPDATE = re.compile(
    r"id:\s*id\((\d+)\),\s*from:\s*\{[^}]*\},\s*to:\s*\{\s*hours:\s*([\d.]+),\s*list_price:\s*([\d.]+)\s*\}"
)


def _id(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def parse_catalog_source(source: str) -> list[CatalogItem]:
    """Every example post type, last definition per id winning, with the value updates applied."""
    rows: dict[int, CatalogItem] = {}
    for n, name, hours, price, section in _ROW.findall(source):
        rows[int(n)] = CatalogItem(
            id=_id(int(n)),
            name=name.replace("\\'", "'"),
            hours=float(hours),
            material=float(price),
            section=int(section),
        )
    for n, hours, price in _UPDATE.findall(source):
        if int(n) in rows:
            rows[int(n)] = rows[int(n)].model_copy(update={"hours": float(hours), "material": float(price)})
    unit = re.compile(r"\((m|st)\)\s*$")
    out = []
    for item in rows.values():
        match = unit.search(item.name)
        out.append(item.model_copy(update={"unit": match.group(1)}) if match else item)
    return out


def export(
    source: Path,
    out: Path,
    hourly_rate: float | None,
    list_discount_pct: float = 0.0,
    material_markup_pct: float = 0.0,
) -> Catalog:
    items = parse_catalog_source(source.read_text(encoding="utf-8"))
    if not items:
        raise ValueError("no catalog rows found in the source")
    catalog = Catalog(
        name="estim-default",
        label="Estims standardkatalog",
        default=True,
        hourly_rate=hourly_rate,
        list_discount_pct=list_discount_pct,
        material_markup_pct=material_markup_pct,
        items=items,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(catalog.model_dump(mode="json"), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return catalog


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, default=APP_SOURCE)
    parser.add_argument("--hourly-rate", type=float, default=None)
    parser.add_argument("--list-discount", type=float, default=0.0)
    parser.add_argument("--markup", type=float, default=0.0)
    args = parser.parse_args(argv)
    out = catalog_path("estim-default")
    catalog = export(args.source, out, args.hourly_rate, args.list_discount, args.markup)
    print(f"wrote {len(catalog.items)} items to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
