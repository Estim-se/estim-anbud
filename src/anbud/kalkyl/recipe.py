"""Recipes: how many of an item one room type holds, for scope the documents set without a count.

Two sources, in order, and every count from one carries its label:

1. The customer's recipe, `~/estim-anbud-data/<company>/references/outlet-recipe.toml`, labelled "kundens recept".
   Its `source` field says where the customer's figures come from and is required.
2. Estim's default recipe, `~/estim-anbud-data/catalogs/estim-default-recipe.json`, exported from the app's example
   values by `anbud.kalkyl.export_recipe` and labelled "Estims standardrecept, ej kundens".

Without either, a per-room requirement with no stated count stays "Saknar mängd" with its multiplier and citations.
No recipe figure is ever written in this repo.

Both files have one shape:

    source = "where the figures come from"
    [per_apartment]          # room types every apartment has one of, when the list doesn't say (optional)
    hall = 1
    [[items]]
    room = "bedroom"         # an ObjectKind: living_room, bedroom, half_room, hall, kitchen, bathroom, wc, ...
    item = "Dubbelt vägguttag"
    catalog_id = "..."       # optional; without it the item name must equal a catalog item's name
    count = 3
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from anbud import paths
from anbud.granskning.catalog import Catalog, CatalogItem
from anbud.kalkyl.records import ObjectKind
from anbud.prep.text import normalise

CUSTOMER_FILE = "references/outlet-recipe.toml"
DEFAULT_NAME = "estim-default-recipe"
CUSTOMER_LABEL = "kundens recept"
DEFAULT_LABEL = "Estims standardrecept, ej kundens"


class RecipeError(ValueError):
    """Raised with a reason that names no customer content."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RecipeItem(_Model):
    room: ObjectKind
    item: str = Field(min_length=1, max_length=120)
    catalog_id: str | None = Field(default=None, max_length=80)
    count: float = Field(ge=0, le=100)


class Recipe(_Model):
    name: str
    label: str
    source: str = Field(min_length=1)
    per_apartment: dict[ObjectKind, float] = Field(default_factory=dict)
    items: list[RecipeItem] = Field(default_factory=list)


def default_path(root: Path | None = None) -> Path:
    return (root or paths.data_root()) / "catalogs" / f"{DEFAULT_NAME}.json"


def customer_path(company_id: str, root: Path | None = None) -> Path:
    return paths.company_dir(company_id, root) / CUSTOMER_FILE


def load_recipe(company_id: str, root: Path | None = None) -> Recipe | None:
    """The customer's recipe if the company has one, else Estim's default, else None. A recipe file that exists but
    doesn't parse is an error, never a silent fallback."""
    customer = customer_path(company_id, root)
    if customer.exists():
        try:
            raw = tomllib.loads(customer.read_text(encoding="utf-8"))
            return Recipe.model_validate({**raw, "name": "customer", "label": CUSTOMER_LABEL})
        except (tomllib.TOMLDecodeError, ValidationError) as exc:
            raise RecipeError(f"the customer's recipe doesn't parse ({type(exc).__name__})") from None
    default = default_path(root)
    if default.exists():
        try:
            return Recipe.model_validate(json.loads(default.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise RecipeError(f"the default recipe doesn't parse ({type(exc).__name__})") from None
    return None


def _dec(value: float) -> Decimal:
    d = Decimal(str(value))
    return d.quantize(Decimal(1)) if d == d.to_integral_value() else d


def _item(catalog: Catalog, entry: RecipeItem) -> CatalogItem | None:
    if entry.catalog_id and (found := catalog.find(entry.catalog_id)) is not None:
        return found
    wanted = normalise(entry.item).casefold()
    return next((i for i in catalog.items if normalise(i.name).casefold() == wanted), None)


@dataclass(frozen=True)
class Count:
    """One recipe figure bound to a catalog item."""

    room: str
    item: CatalogItem
    count: Decimal
    label: str


@dataclass
class BoundRecipe:
    """A recipe whose items are matched to this run's catalog. Items that match no catalog item can't be used and are
    counted in `unbound`."""

    label: str
    source: str
    per_apartment: dict[str, Decimal]
    counts: list[Count]
    unbound: int

    def count_for(self, room: str, catalog_id: str | None) -> Count | None:
        if catalog_id is None:
            return None
        return next((c for c in self.counts if c.room == room and c.item.id == catalog_id), None)

    def for_rooms(self, rooms: set[str]) -> list[Count]:
        return [c for c in self.counts if c.room in rooms and c.count > 0]


def bind(recipe: Recipe | None, catalog: Catalog) -> BoundRecipe | None:
    if recipe is None:
        return None
    counts, unbound = [], 0
    for entry in recipe.items:
        item = _item(catalog, entry)
        if item is None:
            unbound += 1
            continue
        counts.append(Count(entry.room, item, _dec(entry.count), recipe.label))
    per = {k: _dec(v) for k, v in recipe.per_apartment.items()}
    return BoundRecipe(recipe.label, recipe.source, per, counts, unbound)
