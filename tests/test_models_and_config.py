from __future__ import annotations

import json
import tomllib

import pytest
from pydantic import ValidationError

from anbud import paths
from anbud.models import Citation, Flag, MoneyBasis, Verdict


def basis() -> MoneyBasis:
    return MoneyBasis(kind="missing_post", item="Synthetic item", quantity=3, unit="st")


def test_money_basis_schema_is_flat() -> None:
    schema = json.dumps(MoneyBasis.model_json_schema())
    assert '"kind"' in schema
    for keyword in ('"oneOf"', '"discriminator"'):
        assert keyword not in schema


def test_flag_needs_a_citation_and_forbids_extra_fields() -> None:
    cite = Citation(block_id="b1", quote="synthetic quote")
    Flag(lens_id="contract", claim_type="contract_vite", claim="synthetic", citations=[cite], money_basis=basis())
    with pytest.raises(ValidationError):
        Flag(lens_id="contract", claim_type="x", claim="y", citations=[], money_basis=basis())
    with pytest.raises(ValidationError):
        Citation(block_id="b1", quote="q", extra="no")


def test_verdict_values_are_closed() -> None:
    with pytest.raises(ValidationError):
        Verdict(fingerprint="f", verdict="maybe", electrical_scope=True, covered_by_calc=False, would_be_wrong_if="x")


def test_config_files_parse_and_every_model_has_prices() -> None:
    config = paths.repo_root() / "config"
    providers = tomllib.loads((config / "providers.toml").read_text())
    prices = tomllib.loads((config / "prices.toml").read_text())
    budgets = tomllib.loads((config / "budgets.toml").read_text())
    for model_id in providers["models"].values():
        assert {"input", "output", "cache_write_1h", "cache_read"} <= set(prices[model_id])
    for role, alias in providers["roles"].items():
        assert alias in providers["models"], role
    assert budgets["run"]["hard_stop"] == 20.0
    tiers = budgets["lens_tiers"]
    assert [t["max_prefix_tokens"] for t in tiers] == sorted(t["max_prefix_tokens"] for t in tiers)


def test_ci_reruns_when_a_pr_title_or_body_is_edited() -> None:
    workflow = (paths.repo_root() / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "types: [opened, synchronize, reopened, edited]" in workflow
    assert 'branches: ["**"]' in workflow  # every branch push
    assert workflow.index("anbud.guards ci") < workflow.index("ruff check")  # lint can't skip the guards
    assert "pull_request_target" not in workflow
    assert "uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in workflow
