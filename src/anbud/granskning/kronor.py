"""Kronor (plan §6.1 step 12): code prices a flag's money basis. No model ever sees a price from here.

- Catalogs (`anbud.granskning.catalog`) load at run time from the data folder; the repo holds no prices. Every figure
  priced from a catalog carries its label, and Estim's default catalog also sets `default_catalog=True`.
- Contract formulas (vite per week, security, index drift, wage step) are computed here from the flag's named inputs;
  `FORMULA_INPUTS` lists the names, for lens prompts.
- The calc's own figures (`CalcPost`) price a quantity gap on a post when they can, before the catalog.
"""

from __future__ import annotations

from anbud.granskning.catalog import Catalog, CatalogItem
from anbud.granskning.contracts import CalcPost
from anbud.granskning.items import Kronor
from anbud.granskning.settings import Settings
from anbud.models import Flag, MoneyBasis

FORMULA_INPUTS: dict[str, dict[str, str]] = {
    "vite_per_week": {
        "weeks": "weeks of delay at risk (required)",
        "per_week": "vite in SEK per week, or give percent_per_week and contract_sum",
        "percent_per_week": "vite per week as a percent of the contract sum",
        "contract_sum": "the contract sum in SEK",
        "cap": "the vite cap in SEK, if stated",
        "cap_percent": "the vite cap as a percent of the contract sum, if stated",
    },
    "security": {
        "contract_sum": "the contract sum in SEK (required)",
        "percent": "the security as a percent of the contract sum (required)",
        "months": "how long the security runs, in months (required)",
        "cost_percent_per_year": "what the guarantee costs per year, percent; config default if left out",
    },
    "index_drift": {
        "material_amount": "the material amount in SEK exposed to price change (required)",
        "change_percent": "the expected index change over the period, percent; or annual_percent and months",
        "annual_percent": "the expected index change per year, percent",
        "months": "months from the bid to purchase",
    },
    "wage_step": {
        "hours": "hours worked after the wage step (required)",
        "increase_per_hour": "the wage increase in SEK per hour; or increase_percent",
        "increase_percent": "the wage increase in percent of the hourly rate",
        "hourly_rate": "the hourly rate in SEK; the catalog's if left out",
    },
}


class _Unpriced(Exception):
    pass


def _inputs(basis: MoneyBasis) -> dict[str, float]:
    return {i.name: i.value for i in basis.formula_inputs}


def _need(inputs: dict[str, float], *names: str) -> list[float]:
    missing = [n for n in names if n not in inputs]
    if missing:
        raise _Unpriced("saknar indata: " + ", ".join(missing))
    return [inputs[n] for n in names]


def _formula(basis: MoneyBasis, catalog: Catalog | None, settings: Settings) -> tuple[float, str, bool]:
    """(amount, basis text, whether the catalog's hourly rate was read)."""
    x = _inputs(basis)
    if basis.formula == "vite_per_week":
        (weeks,) = _need(x, "weeks")
        if "per_week" in x:
            per_week = x["per_week"]
        else:
            pct, total = _need(x, "percent_per_week", "contract_sum")
            per_week = total * pct / 100
        amount = per_week * weeks
        cap = x.get("cap")
        if cap is None and "cap_percent" in x and "contract_sum" in x:
            cap = x["contract_sum"] * x["cap_percent"] / 100
        capped = cap is not None and amount > cap
        amount = min(amount, cap) if cap is not None else amount
        return amount, f"Vite: {weeks:g} veckor" + (", begränsat av vitestaket" if capped else ""), False
    if basis.formula == "security":
        total, pct, months = _need(x, "contract_sum", "percent", "months")
        cost = x.get("cost_percent_per_year", settings.guarantee_cost_percent_per_year)
        amount = total * pct / 100 * cost / 100 * months / 12
        return amount, f"Säkerhet: {pct:g} % av kontraktssumman i {months:g} månader, kostnad {cost:g} % per år", False
    if basis.formula == "index_drift":
        (material,) = _need(x, "material_amount")
        if "change_percent" in x:
            pct = x["change_percent"]
        else:
            annual, months = _need(x, "annual_percent", "months")
            pct = annual * months / 12
        return material * pct / 100, f"Indexförändring: {pct:g} % på materialbeloppet", False
    if basis.formula == "wage_step":
        (hours,) = _need(x, "hours")
        read_rate = False
        if "increase_per_hour" in x:
            increase = x["increase_per_hour"]
        else:
            (pct,) = _need(x, "increase_percent")
            if "hourly_rate" in x:
                rate = x["hourly_rate"]
            else:
                rate, read_rate = _rate(catalog), True
            if not rate:
                raise _Unpriced("saknar timpris")
            increase = rate * pct / 100
        return hours * increase, f"Lönesteg: {hours:g} timmar efter lönehöjningen", read_rate
    raise _Unpriced("okänd avtalsformel")


def _rate(catalog: Catalog | None) -> float:
    if catalog is None or catalog.hourly_rate <= 0:
        raise _Unpriced("katalogen saknar timpris")
    return catalog.hourly_rate


def _catalog_cost(basis: MoneyBasis, catalog: Catalog | None, post: CalcPost | None) -> tuple[CatalogItem, float]:
    """The catalog item the basis names (or the calc post's text names) and its unit cost."""
    if catalog is None:
        raise _Unpriced("ingen katalog")
    item = catalog.lookup(basis.item) or (catalog.lookup(post.text) if post else None)
    if item is None:
        raise _Unpriced("posten finns inte i katalogen")
    cost = catalog.unit_cost(item)
    if cost is None:
        raise _Unpriced("katalogposten saknar pris, eller katalogen saknar timpris")
    return item, cost


Source = str | None  # "catalog" (an item or the catalog's rate), "calc" (the calc's own rate), or None


def _rate_source(catalog: Catalog | None) -> Source:
    return "calc" if catalog is not None and catalog.hourly_rate_from_calc else "catalog"


def _priced(
    basis: MoneyBasis, catalog: Catalog | None, posts: dict[str, CalcPost], settings: Settings
) -> tuple[float, str, Source]:
    """(point, basis text, where any rate or price came from)."""
    post = posts.get(basis.post_ref) if basis.post_ref else None
    if basis.kind == "contract_formula":
        amount, text, read_rate = _formula(basis, catalog, settings)
        return amount, text, _rate_source(catalog) if read_rate else None
    if basis.kind == "hours_gap":
        if basis.hours is None:
            raise _Unpriced("saknar timmar")
        return basis.hours * _rate(catalog), f"Timmar som fattas: {basis.hours:g} h", _rate_source(catalog)
    if basis.kind == "quantity_gap":
        if basis.quantity is None:
            raise _Unpriced("saknar mängd")
        unit = basis.unit or (post.unit if post else None) or "st"
        if post is not None and post.amount is not None and post.quantity:
            return basis.quantity * post.amount / post.quantity, f"Mängd som fattas: {basis.quantity:g} {unit}", None
        if post is not None and post.unit_price is not None:
            return basis.quantity * post.unit_price, f"Mängd som fattas: {basis.quantity:g} {unit}", None
        item, cost = _catalog_cost(basis, catalog, post)
        return basis.quantity * cost, f"Mängd som fattas: {basis.quantity:g} {unit} {item.name}", "catalog"
    # missing_post
    quantity = basis.quantity if basis.quantity is not None else 1.0
    try:
        item, cost = _catalog_cost(basis, catalog, None)
    except _Unpriced:
        if basis.hours is not None:
            return basis.hours * _rate(catalog), f"Saknad post: {basis.hours:g} h arbete", _rate_source(catalog)
        raise
    return quantity * cost, f"Saknad post: {quantity:g} {basis.unit or item.unit} {item.name}", "catalog"


def price(
    basis: MoneyBasis,
    catalog: Catalog | None,
    calc_posts: dict[str, CalcPost] | None = None,
    settings: Settings | None = None,
) -> Kronor:
    settings = settings or Settings()
    try:
        point, text, origin = _priced(basis, catalog, calc_posts or {}, settings)
    except _Unpriced as why:
        return Kronor(priced=False, basis=basis, reason=str(why))
    spread = settings.spread.get(basis.kind, 0.0)
    source = catalog if origin == "catalog" else None
    if source is not None:
        text += f" ({source.label})"
    elif origin == "calc" and catalog is not None:
        text += f" (timpris {catalog.hourly_rate:g} kr/h från kundens kalkyl)"
    return Kronor(
        priced=True,
        point=round(point),
        low=round(point * (1 - spread)),
        high=round(point * (1 + spread)),
        basis=basis,
        label=text,
        catalog=source.name if source else None,
        default_catalog=bool(source and source.default),
    )


def is_minor(kronor: Kronor | None, flag: Flag, settings: Settings) -> bool:
    """Under the materiality threshold and no contractual exposure. Unpriced flags are never minor."""
    if flag.contractual_exposure or kronor is None or not kronor.priced or kronor.high is None:
        return False
    return kronor.high < settings.materiality_sek


def ranking_kronor(kronor: Kronor | None, settings: Settings) -> float:
    """Kronor high for ranking; an unpriced flag counts as `unpriced_sek`."""
    if kronor is None or not kronor.priced or kronor.high is None:
        return settings.unpriced_sek
    return kronor.high
