"""The price and time catalog that kronor and Kalkyl price against. Loaded at run time from
`~/estim-anbud-data/catalogs/<name>.json`; the repo holds no prices.

    {"name": "estim-default", "label": "Estims standardkatalog (ej kundens)", "currency": "SEK", "hourly_rate": 0.0,
     "items": [{"id": "...", "name": "...", "unit": "st", "unit_price": 0.0, "hours": 0.0, "category": "..."}]}

- `unit_price` is the material (list) price per unit in SEK, without labour; `hours` is the labour per unit. One unit
  costs `hours x hourly_rate + unit_price`. Either may be null.
- `hourly_rate` 0 means the rate isn't set: hours can't be priced then.
- Every figure priced from a catalog carries its `label` wherever it is shown.
"""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from anbud.granskning.contracts import CalcPost
from anbud.paths import data_root
from anbud.prep.text import normalise

DEFAULT_CATALOG = "estim-default"
CATALOG_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_TOKEN = re.compile(r"\w+")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CatalogItem(_Model):
    id: str = Field(description="Stable id")
    name: str
    unit: str = "st"
    unit_price: float | None = Field(default=None, ge=0, description="Material list price per unit, SEK, no labour")
    hours: float | None = Field(default=None, ge=0, description="Labour hours per unit")
    category: str | None = None


def _key(text: str) -> str:
    return normalise(text).casefold()


class Catalog(_Model):
    name: str
    label: str = Field(description="Shown next to every figure priced from it")
    currency: str = "SEK"
    hourly_rate: float = Field(ge=0, description="SEK per hour; 0 means not set")
    hourly_rate_from_calc: bool = Field(
        default=False, description="Set at run time when the rate is the customer's own, from their calc"
    )
    items: list[CatalogItem] = Field(default_factory=list)

    @property
    def default(self) -> bool:
        """Estim's default catalog, not the customer's own."""
        return self.name == DEFAULT_CATALOG

    def find(self, item_id: str) -> CatalogItem | None:
        return next((i for i in self.items if i.id == item_id), None)

    def search(self, text: str) -> list[CatalogItem]:
        """Items whose name holds every word of `text`, case-insensitively, in catalog order."""
        wanted = set(_TOKEN.findall(_key(text)))
        if not wanted:
            return []
        return [i for i in self.items if wanted <= set(_TOKEN.findall(_key(i.name)))]

    def lookup(self, wanted: str | None) -> CatalogItem | None:
        """The one item a money basis means: by id, then by exact name, then by a close name (difflib 0.85)."""
        if not wanted:
            return None
        if (item := self.find(wanted)) is not None:
            return item
        names = {_key(i.name): i for i in self.items}
        if _key(wanted) in names:
            return names[_key(wanted)]
        close = difflib.get_close_matches(_key(wanted), list(names), n=1, cutoff=0.85)
        return names[close[0]] if close else None

    def unit_cost(self, item: CatalogItem) -> float | None:
        """Labour plus material for one unit, or None when it can't be priced (no figures, or hours without a rate)."""
        if item.unit_price is None and item.hours is None:
            return None
        if item.hours and self.hourly_rate <= 0:
            return None
        return (item.hours or 0.0) * self.hourly_rate + (item.unit_price or 0.0)


_RATE_POST = re.compile(
    r"\btimpris|timkostnad|timdebitering|montörstim|arbetstim|\bkr\s*/\s*(?:tim|h)\b|per\s+(?:arbets)?timme",
    re.IGNORECASE,
)
_ASSUMED_RATE = re.compile(r",?\s*antaget timpris[^,]*(?:,\s*ej kundens)?")
MIN_RATE, MAX_RATE = 200.0, 3000.0


def calc_hourly_rate(posts: Iterable[CalcPost]) -> float | None:
    """The hourly rate the customer's calc states, if a post names one: its unit price, or its amount when the post
    is a single unit. The first plausible one (200-3000 kr) wins."""
    for post in posts:
        if not _RATE_POST.search(f"{post.section or ''} {post.text}"):
            continue
        value = post.unit_price
        if value is None and post.amount is not None and post.quantity in (None, 1):
            value = post.amount
        if value is not None and MIN_RATE <= value <= MAX_RATE:
            return value
    return None


def with_calc_rate(catalog: Catalog, posts: Iterable[CalcPost]) -> Catalog:
    """The catalog with the calc's own hourly rate, when the calc states one, and a label that says so."""
    rate = calc_hourly_rate(posts)
    if rate is None:
        return catalog
    label = _ASSUMED_RATE.sub("", catalog.label) + f", timpris {rate:g} kr/h från kundens kalkyl"
    return catalog.model_copy(update={"hourly_rate": rate, "label": label, "hourly_rate_from_calc": True})


def catalog_path(name: str, root: Path | None = None) -> Path:
    if not CATALOG_NAME.fullmatch(name):
        raise ValueError("catalog names look like estim-default")
    return (root or data_root()) / "catalogs" / f"{name}.json"


def load_catalog(path: Path) -> Catalog:
    return Catalog.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def load_named_catalog(name: str, root: Path | None = None) -> Catalog:
    return load_catalog(catalog_path(name, root))
