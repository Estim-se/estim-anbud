"""Kalkyl's view of the shared catalog (`anbud.granskning.catalog`): loading by name with errors that name no
content, and the one view of a catalog that goes into a prompt: ids, names, units and categories, never a price or an
hour."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from anbud.granskning.catalog import Catalog, load_named_catalog


class CatalogError(RuntimeError):
    pass


def load(name: str, root: Path | None = None) -> Catalog:
    """The named catalog. Its ids must be unique: R2 and R3 choose items by id."""
    try:
        catalog = load_named_catalog(name, root)
    except FileNotFoundError:
        raise CatalogError(f"no catalog named {name} in the data folder") from None
    except ValidationError as exc:
        raise CatalogError(f"the catalog {name} doesn't match the format ({exc.error_count()} problem(s))") from None
    except (OSError, ValueError):
        raise CatalogError(f"the catalog {name} can't be read (name or JSON)") from None
    if len(set(ids(catalog))) != len(catalog.items):
        raise CatalogError(f"the catalog {name} has an item id twice")
    return catalog


def ids(catalog: Catalog) -> list[str]:
    return [i.id for i in catalog.items]


def prompt_lines(catalog: Catalog) -> str:
    """The catalog as a prompt sees it: `id | name | unit | category`. Never a price or an hour."""
    return "\n".join(f"{i.id} | {i.name} | {i.unit} | {i.category or ''}" for i in catalog.items)
