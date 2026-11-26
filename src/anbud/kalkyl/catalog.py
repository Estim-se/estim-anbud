"""A minimal catalog loader, to be swapped for `anbud.granskning.catalog` when it lands. Same file format:

    {"name", "label", "currency", "hourly_rate", "items": [{"id", "name", "unit", "unit_price", "hours", "category"}]}

read from `~/estim-anbud-data/catalogs/<name>.json` at run time. No prices live in the repo, and `prompt_lines()` is
the only view of a catalog that goes into a prompt: ids, names, units and categories, never a price or an hour.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from anbud.paths import data_root

CATALOG_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DEFAULT_CATALOG = "estim-default"


class CatalogError(RuntimeError):
    pass


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CatalogItem(_Model):
    id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=240)
    unit: str = Field(min_length=1, max_length=20)
    unit_price: Decimal = Field(ge=0)
    hours: Decimal = Field(ge=0, description="Hours per unit")
    category: str = Field(default="", max_length=120)

    @field_validator("id")
    @classmethod
    def _plain_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
            raise ValueError("catalog item ids are letters, digits, dot, dash and underscore")
        return value


class Catalog(_Model):
    name: str
    label: str = Field(min_length=1, max_length=200, description="Shown on every sheet of the calc")
    currency: str = "SEK"
    hourly_rate: Decimal = Field(ge=0)
    items: list[CatalogItem]

    @field_validator("items")
    @classmethod
    def _unique(cls, items: list[CatalogItem]) -> list[CatalogItem]:
        ids = [i.id for i in items]
        if len(set(ids)) != len(ids):
            raise ValueError("catalog item ids must be unique")
        return items

    def item(self, item_id: str | None) -> CatalogItem | None:
        if not item_id:
            return None
        return next((i for i in self.items if i.id == item_id), None)

    @property
    def ids(self) -> list[str]:
        return [i.id for i in self.items]

    def prompt_lines(self) -> str:
        """The catalog as a prompt sees it: `id | name | unit | category`. Never a price or an hour."""
        return "\n".join(f"{i.id} | {i.name} | {i.unit} | {i.category}" for i in self.items)


def catalog_path(name: str, root: Path | None = None) -> Path:
    if not CATALOG_NAME.fullmatch(name):
        raise CatalogError("catalog names are lower-case letters, digits, dash and underscore")
    return (root or data_root()) / "catalogs" / f"{name}.json"


def load_catalog(name: str = DEFAULT_CATALOG, root: Path | None = None) -> Catalog:
    path = catalog_path(name, root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    except FileNotFoundError:
        raise CatalogError(f"no catalog named {name} in the data folder") from None
    except (OSError, ValueError):
        raise CatalogError(f"the catalog {name} can't be read as JSON") from None
    try:
        return Catalog.model_validate(raw)
    except ValidationError as exc:
        raise CatalogError(f"the catalog {name} doesn't match the format ({exc.error_count()} problem(s))") from None
