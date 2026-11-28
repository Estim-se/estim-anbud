"""Per-run hard stops by tender size: a size at a tier's bound picks the lower tier, a run never spends above the tier
its dry run gave, and the tier each run picked is in its run record and its report. Synthetic tenders only."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import openpyxl
import pytest
from granskning_fakes import ScriptedSDK, png_bytes, usage, write_prep
from test_granskning_pipeline import CLEAN, _calc, _catalog, script
from test_runguard import ledger_with

from anbud import cli, model_client
from anbud import config as config_module
from anbud.config import load_config
from anbud.evals import split_heldout as sh
from anbud.evals.exam import hard_stops, tender_stop
from anbud.granskning import provenance
from anbud.model_client import Budget
from anbud.stops import ESTIMATE, MEASURED, Tier, granskning_stop, granskning_tiers, kalkyl_stop, kalkyl_tiers, pick
from anbud.store import RunStore

BUDGETS = load_config().budgets


@pytest.mark.parametrize(
    ("tokens", "tier", "usd"),
    [(1, "S", 20), (150_000, "S", 20), (150_001, "M", 30), (300_000, "M", 30), (300_001, "L", 45), (450_000, "L", 45)],
)
def test_a_prefix_at_a_tiers_bound_picks_the_lower_tier(tokens: int, tier: str, usd: float) -> None:
    stop = granskning_stop(BUDGETS, tokens, MEASURED)
    assert (stop.tier, stop.usd, stop.above_top) == (tier, usd, False)


@pytest.mark.parametrize(
    ("high", "tier", "usd"),
    [(1.0, "S", 3), (2.0, "S", 3), (2.01, "M", 8), (16 / 3, "M", 8), (10.0, "L", 25), (50 / 3, "L", 25)],
)
def test_a_kalkyl_estimate_at_a_tiers_bound_picks_the_lower_tier(high: float, tier: str, usd: float) -> None:
    stop = kalkyl_stop(kalkyl_tiers(BUDGETS), high, 1.5)  # $2.00 x 1.5 = $3.00, the S tier's stop exactly
    assert (stop.tier, stop.usd, stop.source) == (tier, usd, ESTIMATE)


def test_a_size_above_the_top_tier_gets_the_top_tier_and_says_so() -> None:
    assert granskning_stop(BUDGETS, 450_001, ESTIMATE).summary() == {
        "tier": "L",
        "tier_usd": 45.0,
        "hard_stop_usd": 45.0,
        "size": 450_001,
        "from": "estimate",
        "above_top_tier": True,
        "floor_usd": 0.0,  # a tier pick alone has no floor (`stops.scaled`)
        "scaled": False,
    }
    assert kalkyl_stop(kalkyl_tiers(BUDGETS), 17.0, 1.5).above_top  # $25.50 needed, over the $25 top tier
    assert not kalkyl_stop(kalkyl_tiers(BUDGETS), 11.0, 1.5).above_top  # $16.50 fits the $25 top tier


def test_the_budget_moves_its_stop_but_never_above_its_ceiling() -> None:
    budget = Budget(30.0)
    assert budget.set_stop(45.0) == 30.0 and budget.set_stop(20.0) == 20.0 and budget.set_stop(30.0) == 30.0
    assert Budget(20.0, ceiling_usd=45.0).set_stop(45.0) == 45.0


def test_the_exam_counts_each_tenders_own_stop_and_the_top_one_when_its_size_is_unknown() -> None:
    assert hard_stops()["granskning"] == 45.0 and hard_stops()["kalkyl"] == 25.0
    assert tender_stop("granskning", 150_000) == 20.0 and tender_stop("granskning", 900_000) == 45.0
    assert tender_stop("kalkyl", 2.0) == 3.0 and tender_stop("kalkyl", 4.0) == 8.0
    assert tender_stop("granskning", None) == 45.0 and tender_stop("kalkyl", None) == 25.0


# --- Granskning: the measured prefix picks the tier, under the ceiling its estimate gave ---------------------------

SMALL = 1000  # tokens: the tiers below put the synthetic tender's estimated prefix in M


def _tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    real = config_module.load_config

    def load(*args, **kwargs):
        config = real(*args, **kwargs)
        config.budgets["granskning_stops"] = [
            {"tier": "S", "max_prefix_tokens": SMALL, "hard_stop": 20.0},
            {"tier": "M", "max_prefix_tokens": 1_000_000, "hard_stop": 30.0},
            {"tier": "L", "max_prefix_tokens": 2_000_000, "hard_stop": 45.0},
        ]
        return config

    monkeypatch.setattr(config_module, "load_config", load)


def _measured(tokens: int):
    def answer(params: dict[str, Any]) -> Any:
        out = script(params)
        if any(getattr(b, "name", None) == "submit_map" for b in out.content):  # the map's first call
            out.usage = usage(write=tokens)
        return out

    return answer


@pytest.fixture
def tender(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(provenance, "git_state", lambda root=None: CLEAN)
    ledger_with(data_root, t_0001=sh.DEV)
    blob = RunStore("co-001").put_blob(png_bytes())
    base = data_root / "co-001" / "dev" / "t-0001"
    write_prep(base / "prep", tile_blob=blob)
    _calc(base / "calc")
    _catalog(data_root)
    monkeypatch.setattr(model_client, "keychain_key", lambda **_: "sk-test")
    _tiers(monkeypatch)
    return base


def _run(
    tender: Path, monkeypatch: pytest.MonkeyPatch, run_id: str, measured: int
) -> tuple[dict[str, Any], dict[Any, Any], str]:
    sdk = ScriptedSDK(_measured(measured))
    monkeypatch.setattr(model_client, "make_sdk_client", lambda config, key: sdk)
    assert cli.main(["run", "co-001", "t-0001", "--run-id", run_id]) == 0
    summary = json.loads((RunStore("co-001").run_dir(run_id) / "granskning.json").read_text())
    report = tender / "runs" / run_id / "report"
    about = {
        r[0].value: r[1].value for r in openpyxl.load_workbook(report / "granskning.xlsx")["Om rapporten"].iter_rows()
    }
    return summary, about, (report / "granskning.html").read_text(encoding="utf-8")


def test_the_measured_prefix_at_a_bound_picks_the_lower_tier_and_the_record_and_report_show_it(
    tender: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["run", "co-001", "t-0001", "--dry-run"]) == 0
    assert "hard stop: tier M, $30.00 at most, from the estimated prefix" in capsys.readouterr().out
    summary, about, page = _run(tender, monkeypatch, "t-0001-small", SMALL)
    assert summary["stop"] == {
        "tier": "S",
        "tier_usd": 20.0,
        "hard_stop_usd": 20.0,
        "size": SMALL,
        "from": "measured",
        "above_top_tier": False,
    } | {"floor_usd": summary["stop"]["floor_usd"], "scaled": False}  # the floor stays below the tier's stop here
    assert 0 < summary["stop"]["floor_usd"] < 20.0
    label = f"S, 20 USD (prefix {SMALL} token, mätt)"
    assert (
        about["Kostnadsstopp (nivå efter storlek)"] == label and f"Kostnadsstopp (nivå efter storlek): {label}." in page
    )
    out = capsys.readouterr().out
    assert "hard stop: tier S, $20.00, from the measured prefix" in out
    assert re.search(r"^budget spent \$[\d.]+ of \$20\.00$", out, re.M)


def test_a_measured_prefix_above_the_estimates_tier_never_raises_the_stop(
    tender: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    summary, about, _ = _run(tender, monkeypatch, "t-0001-large", 1_500_000)
    assert summary["stop"]["tier"] == "L" and summary["stop"]["tier_usd"] == 45.0
    assert summary["stop"]["hard_stop_usd"] == 30.0  # the M tier the run opened with
    assert about["Kostnadsstopp (nivå efter storlek)"].endswith("; högst 30 USD, nivån körningen startade med")
    assert re.search(r"^budget spent \$[\d.]+ of \$30\.00$", capsys.readouterr().out, re.M)


# --- Kalkyl: the dry run's tier is the run's ------------------------------------------------------------------------


def test_kalkyl_picks_its_tier_from_its_dry_run_before_any_call_and_shows_it(data_root: Path) -> None:
    from kalkyl_synthetic import fake_sdk, write_catalog
    from kalkyl_synthetic import write_prep as write_kalkyl_prep

    from anbud import paths
    from anbud.kalkyl.run import plan, run_dev_tender
    from anbud.kalkyl.settings import KalkylConfig, load_kalkyl_config

    tender = paths.dev_tender_dir("co-001", "t-0001")
    write_kalkyl_prep(tender / "prep")
    write_catalog(data_root)
    dry = plan("co-001", "t-0001", catalog_name="synthetic")
    need = round(dry["cost_usd_est_high"] * 1.5, 4)
    assert dry["stop_tier"] == "S" and dry["hard_stop_usd"] == 3.0 and dry["stop_picked_from_usd"] == need
    # Tiers around this tender's estimate: it sits at M's bound exactly, so it picks M, not L.
    tiers = (Tier("S", need / 2, 3.0), Tier("M", need, 8.0), Tier("L", need * 2, 15.0))
    assert pick(tiers, need, ESTIMATE).tier == "M"
    kalkyl = KalkylConfig(**{**vars(load_kalkyl_config()), "stops": tiers})
    summary = run_dev_tender("co-001", "t-0001", sdk=fake_sdk(), catalog_name="synthetic", run_id="k-m", kalkyl=kalkyl)
    assert summary["complete"] and summary["stop"]["tier"] == "M" and summary["stop"]["hard_stop_usd"] == 8.0
    assert summary["stop"]["cost_usd_est_high"] == dry["cost_usd_est_high"] and summary["stop"]["margin"] == 1.5
    rows = {
        r[0].value: r[1].value
        for r in openpyxl.load_workbook(tender / "kalkyl" / "k-m" / "kalkyl.xlsx")["Översikt"].iter_rows()
    }
    assert (
        rows["Kostnadsstopp för körningen (nivå efter storlek)"]
        == f"M, 8 USD (uppskattning med marginal {need:.2f} USD)"
    )


def test_the_tier_tables_parse_in_order() -> None:
    assert [t.name for t in granskning_tiers(BUDGETS)] == ["S", "M", "L"]
    assert [t.bound for t in kalkyl_tiers(BUDGETS)] == [3.0, 8.0, 25.0]
