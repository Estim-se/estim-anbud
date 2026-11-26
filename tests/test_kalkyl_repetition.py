"""Repetition: tiered factors on hours within one post, the customer's table before Estim's default. Synthetic, $0."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from anbud import paths
from anbud.config import CONFIG_DIR
from anbud.granskning.catalog import Catalog
from anbud.kalkyl.price import Post, price_posts
from anbud.kalkyl.repetition import CUSTOMER_LABEL, Repetition, RepetitionError, load_repetition, parse

TIERS = Repetition("test", ((1, Decimal(1)), (11, Decimal("0.9")), (51, Decimal("0.8"))))


def test_the_factor_is_tiered_like_tax_brackets() -> None:
    assert TIERS.hours_factor(Decimal(10)) == 1  # the first units carry their full setup time
    assert TIERS.hours_factor(Decimal(20)) == Decimal("0.95")  # 10 × 1 + 10 × 0.9, over 20
    assert TIERS.hours_factor(Decimal(100)) == Decimal("0.86")  # 10 + 40 × 0.9 + 50 × 0.8 = 86, over 100
    assert TIERS.hours_factor(Decimal(0)) == 1


@pytest.mark.parametrize(
    "tiers",
    [[], [[2, 1.0]], [[1, 1.0], [1, 0.9]], [[1, 0.9], [10, 1.0]], [[1, 1.2]], [[1, 0.0]], [["1", 1.0]]],
)
def test_a_bad_table_is_refused(tiers: list) -> None:
    with pytest.raises(RepetitionError):
        parse({"tiers": tiers}, "x")


def test_the_customers_table_comes_before_estims_default(tmp_path: Path) -> None:
    default = load_repetition("co-001", tmp_path, CONFIG_DIR)
    assert default is not None and "ej kundens" in default.label
    path = paths.company_dir("co-001", tmp_path) / "references" / "repetition.toml"
    path.parent.mkdir(parents=True)
    path.write_text("tiers = [[1, 1.0], [5, 0.5]]\n")
    customer = load_repetition("co-001", tmp_path, CONFIG_DIR)
    assert customer is not None and customer.label == CUSTOMER_LABEL and customer.tiers[1] == (5, Decimal("0.5"))
    path.write_text("tiers = [[")
    with pytest.raises(RepetitionError):
        load_repetition("co-001", tmp_path, CONFIG_DIR)


def _catalog() -> Catalog:
    return Catalog.model_validate(
        {
            "name": "t",
            "label": "Syntetisk",
            "currency": "SEK",
            "hourly_rate": 500,
            "items": [
                {"id": "u", "name": "Uttag", "unit": "st", "unit_price": 100, "hours": 1.0},
                {"id": "k", "name": "Kabel", "unit": "m", "unit_price": 10, "hours": 0.1},
            ],
        }
    )


def _post(catalog_id: str, quantity: int, unit: str) -> Post:
    return Post(
        id=catalog_id,
        name=catalog_id,
        scope="included",
        unit=unit,
        type_code=None,
        catalog_id=catalog_id,
        lines=[],
        quantity=Decimal(quantity),
    )


def test_a_count_post_takes_the_factor_on_hours_only_and_says_so() -> None:
    outlet, cable = _post("u", 20, "st"), _post("k", 200, "m")
    price_posts([outlet, cable], _catalog(), TIERS)
    assert outlet.hours_flat == 20 and outlet.hours == 19 and outlet.repetition_factor == Decimal("0.95")
    assert outlet.material == 2000 and outlet.labour == 9500 and outlet.total == 11500  # material never changes
    assert "Serieeffekt: timmar × 0,95 i snitt för 20 st (test)" in outlet.notes
    assert outlet.installed_unit_price == 575  # (2000 + 9500) / 20
    assert cable.repetition_factor is None and cable.hours == cable.hours_flat == 20  # a length isn't repetition
    flat = _post("u", 20, "st")
    price_posts([flat], _catalog())
    assert flat.hours == 20 and flat.repetition_factor is None and flat.installed_unit_price == 600
