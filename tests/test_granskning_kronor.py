"""Kronor on a synthetic catalog. No real prices anywhere."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _granskning_synth import CALC_POSTS, basis, catalog, flag

from anbud.granskning.kronor import Catalog, catalog_path, is_minor, load_catalog, price, ranking_kronor
from anbud.granskning.settings import Settings, load_settings

S = load_settings()
POSTS = {p.id: p for p in CALC_POSTS}


def test_settings_load_from_config() -> None:
    assert S.materiality_sek == 20000 and S.max_shown == 15
    assert S.verifier_top_n == 25 and S.verifier_sampled == 3
    assert S.weight("confirmed") == 1.0 and S.weight(None) == S.verdict_weights["unverified"]


def test_a_missing_post_is_priced_from_the_catalog_and_labelled_default() -> None:
    k = price(basis(item="armatur trapphus", quantity=24), catalog(), POSTS, S)
    assert k.priced and k.point == 24 * (1 * 500 + 1000)
    assert k.low == round(k.point * 0.75) and k.high == round(k.point * 1.25)
    assert k.default_catalog and "Syntetisk katalog" in k.basis and "Armatur trapphus" in k.basis
    assert price(basis(item="Armatur trapphuset"), catalog(), POSTS, S).priced  # a close name
    assert not price(basis(item="Laddbox"), catalog(), POSTS, S).priced
    own = price(basis(item="Vägguttag"), catalog(default=False), POSTS, S)
    assert own.priced and not own.default_catalog


def test_quantity_gaps_prefer_the_calcs_own_figures() -> None:
    from_amount = price(basis(kind="quantity_gap", post_ref="calc-1", quantity=5), catalog(), POSTS, S)
    assert from_amount.point == 5 * 4000 / 10 and not from_amount.default_catalog and from_amount.catalog is None
    from_unit = price(basis(kind="quantity_gap", post_ref="calc-2", quantity=4), catalog(), POSTS, S)
    assert from_unit.point == 4 * 1500
    from_catalog = price(basis(kind="quantity_gap", item="Vägguttag", quantity=10), catalog(), POSTS, S)
    assert from_catalog.point == 10 * (0.5 * 500 + 100) and from_catalog.default_catalog


def test_hours_need_an_hourly_rate() -> None:
    assert price(basis(kind="hours_gap", hours=40), catalog(), POSTS, S).point == 40 * 500
    k = price(basis(kind="hours_gap", hours=40), catalog(hourly_rate=None), POSTS, S)
    assert not k.priced and "timpris" in k.reason
    assert not price(basis(kind="hours_gap"), catalog(), POSTS, S).priced


@pytest.mark.parametrize(
    ("formula", "inputs", "expected"),
    [
        ("vite_per_week", {"per_week": 5000, "weeks": 6}, 30000),
        ("vite_per_week", {"percent_per_week": 0.5, "contract_sum": 2_000_000, "weeks": 4}, 40000),
        ("vite_per_week", {"per_week": 5000, "weeks": 20, "cap": 50000}, 50000),
        ("vite_per_week", {"per_week": 5000, "weeks": 20, "cap_percent": 2, "contract_sum": 1_000_000}, 20000),
        ("security", {"contract_sum": 4_000_000, "percent": 10, "months": 24}, 4_000_000 * 0.10 * 0.01 * 2),
        ("security", {"contract_sum": 4_000_000, "percent": 10, "months": 12, "cost_percent_per_year": 2}, 8000),
        ("index_drift", {"material_amount": 1_000_000, "change_percent": 3}, 30000),
        ("index_drift", {"material_amount": 1_000_000, "annual_percent": 4, "months": 6}, 20000),
        ("wage_step", {"hours": 1000, "increase_per_hour": 15}, 15000),
        ("wage_step", {"hours": 1000, "increase_percent": 3}, 1000 * 500 * 0.03),
    ],
)
def test_contract_formulas(formula: str, inputs: dict, expected: float) -> None:
    k = price(basis(kind="contract_formula", formula=formula, formula_inputs=inputs), catalog(), POSTS, S)
    assert k.priced and k.point == round(expected) and k.low == k.high == k.point


def test_a_formula_missing_inputs_is_unpriced_with_the_names() -> None:
    k = price(basis(kind="contract_formula", formula="security", formula_inputs={"percent": 10}), None, POSTS, S)
    assert not k.priced and k.reason == "saknar indata: contract_sum, months"


def test_materiality() -> None:
    small = price(basis(kind="hours_gap", hours=10), catalog(), POSTS, S)
    assert is_minor(small, flag(), S)
    assert not is_minor(small, flag(contractual_exposure=True), S)
    unpriced = price(basis(kind="hours_gap"), catalog(), POSTS, S)
    assert not is_minor(unpriced, flag(), S) and ranking_kronor(unpriced, S) == S.unpriced_sek
    assert Settings().materiality_sek == 20000


def test_catalogs_load_from_the_data_folder_only(data_root: Path) -> None:
    path = catalog_path("synthetic")
    assert path == data_root / "catalogs" / "synthetic.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(catalog().model_dump(mode="json")), encoding="utf-8")
    assert load_catalog("synthetic") == catalog()
    with pytest.raises(ValueError):
        catalog_path("../escape")
    assert isinstance(load_catalog("synthetic"), Catalog)
