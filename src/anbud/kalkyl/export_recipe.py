"""Export Estim's default room recipe from the app repo into `~/estim-anbud-data/catalogs/estim-default-recipe.json`.

    uv run python -m anbud.kalkyl.export_recipe

Reads the app's example room recipe read-only (double wall sockets and ceiling outlets per room type; for the kitchen,
counter sockets) and writes a `Recipe` (the format in `anbud.kalkyl.recipe`) labelled as Estim's default, not the
customer's. The app marks these figures as example values the company replaces with its own; the label and the source
note say so. Nothing it reads is written into this repo.

- Room names map to the object kinds Kalkyl counts (living room, bedroom, half room, hall, kitchen).
- The items get the app's own post names, matched by exact name to the default catalog's items: double wall socket,
  counter socket, ceiling outlet. A name the catalog lacks is written without a catalog id.
- One hall and one kitchen per apartment, as the app's object count gives every apartment row one of each.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from anbud.granskning.catalog import DEFAULT_CATALOG, Catalog, catalog_path, load_catalog
from anbud.kalkyl.recipe import DEFAULT_LABEL, DEFAULT_NAME, Recipe, RecipeItem, default_path
from anbud.prep.text import normalise

APP_FILE = "src/lib/tender-room-recipes.ts"
APP_SOURCE = Path.home() / "Documents/estim/workspace/estim" / APP_FILE
_ROOM = re.compile(r"room:\s*'([^']+)'(?:\s+as\s+const)?\s*,\s*sockets:\s*(\d+)\s*,\s*lamps:\s*(\d+)")
ROOMS = {"Vardagsrum": "living_room", "Sovrum": "bedroom", "Halvrum": "half_room", "Hall": "hall", "Kök": "kitchen"}
# The app's post names for recipe equipment, and the default catalog item each one is.
SOCKETS, COUNTER, LAMPS = "Dubbelt vägguttag", "Bänkuttag", "Lamputtag"


def parse_recipe_source(source: str) -> dict[str, tuple[int, int]]:
    """Room name -> (sockets, lamps) for every room the example recipe defines."""
    found = {room: (int(s), int(lamps)) for room, s, lamps in _ROOM.findall(source)}
    if set(found) != set(ROOMS):
        raise ValueError(f"expected the rooms {sorted(ROOMS)} in the source, found {sorted(found)}")
    return found


def _catalog_id(catalog: Catalog | None, name: str) -> str | None:
    if catalog is None:
        return None
    wanted = normalise(name).casefold()
    return next((i.id for i in catalog.items if normalise(i.name).casefold() == wanted), None)


def build(source_text: str, catalog: Catalog | None) -> Recipe:
    rooms = parse_recipe_source(source_text)
    items = []
    for room, (sockets, lamps) in rooms.items():
        kind = ROOMS[room]
        for name, count in ((COUNTER if kind == "kitchen" else SOCKETS, sockets), (LAMPS, lamps)):
            if count > 0:
                items.append(RecipeItem(room=kind, item=name, catalog_id=_catalog_id(catalog, name), count=count))
    digest = hashlib.sha256(source_text.encode()).hexdigest()[:12]
    note = (
        f"Estim-appen {APP_FILE} (exampleRoomRecipes, i appen märkta som exempelvärden som företaget ersätter), "
        f"källfilens sha256 {digest}. Hall och kök: ett per lägenhet som appens objekträkning. "
        f"Katalog-id efter exakt namn i {catalog.name if catalog else 'ingen katalog'}."
    )
    return Recipe(
        name=DEFAULT_NAME,
        label=DEFAULT_LABEL,
        source=note,
        per_apartment={"hall": 1, "kitchen": 1},
        items=items,
    )


def export(source: Path, out: Path, catalog: Catalog | None) -> Recipe:
    recipe = build(source.read_text(encoding="utf-8"), catalog)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(recipe.model_dump(mode="json"), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return recipe


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, default=APP_SOURCE)
    args = parser.parse_args(argv)
    path = catalog_path(DEFAULT_CATALOG)
    catalog = load_catalog(path) if path.exists() else None
    out = default_path()
    recipe = export(args.source, out, catalog)
    bound = sum(1 for i in recipe.items if i.catalog_id)
    print(f"wrote {len(recipe.items)} recipe items ({bound} with a catalog id) to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
