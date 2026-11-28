"""Scaled hard stops (a draft for the founder's decision): each run's stop is raised to a floor scaled to its size, so a
cap never cuts off normal work, never below its tier's stop, fixed before the first paid call from the numbers its dry
run prints (`anbud.stops`). The exam records the same function's stop as each run's cap. Synthetic tenders and the
dev tender's counts only, $0.

Each test names the one-line mutation it catches; each was made, seen to fail the test, and reverted."""

from __future__ import annotations

import copy
import json
import re
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from kalkyl_synthetic import fake_sdk, write_catalog, write_prep
from test_baseline import FINISHING, NOW, dev_tender, fake_app
from test_stops import SMALL, _run, tender  # noqa: F401 - the Granskning fixture

from anbud import cli, paths
from anbud import config as config_module
from anbud.config import load_config
from anbud.evals import baseline
from anbud.kalkyl.run import plan, run_dev_tender
from anbud.kalkyl.settings import KalkylConfig, load_kalkyl_config
from anbud.model_client import Budget
from anbud.stops import (
    Tier,
    baseline_run_stop,
    cards_and_map_usd,
    granskning_run_stop,
    kalkyl_run_stop,
)

CONFIG = load_config()
BUDGETS = CONFIG.budgets

# The dev tender, counts only: its Kalkyl dry run's high estimate without and with the group pass, its Granskning dry
# run's estimated prefix and page-card tiles, and its prep's pages.
DEV_KALKYL_HIGH = (2.99, 7.93)
DEV_PREFIX, DEV_TILES = 128_100, 8
DEV_PAGES = 83


def test_the_dev_tenders_own_stops_stay_at_their_tiers() -> None:
    """Where the tier already covers a run, nothing changes. Mutation: `return max(self.usd, self.floor)` -> `return
    self.floor` in `Stop.opening`: a stop below its tier's."""
    kalkyl = load_kalkyl_config()
    assert [(s.tier, s.hard_stop) for s in (kalkyl_run_stop(kalkyl, h) for h in DEV_KALKYL_HIGH)] == [
        ("M", 8.0),
        ("L", 25.0),
    ]
    assert all(not s.summary()["scaled"] for s in (kalkyl_run_stop(kalkyl, h) for h in DEV_KALKYL_HIGH))
    granskning = granskning_run_stop(CONFIG, DEV_PREFIX, DEV_TILES)
    assert (granskning.tier, granskning.hard_stop) == ("S", 20.0) and 0 < granskning.floor < 20.0
    assert baseline_run_stop(BUDGETS, DEV_PAGES) == 10.0


def test_kalkyl_s_stop_rises_to_its_high_estimate_times_the_spend_ratio() -> None:
    """Mutation: `return scaled(stop, high_usd * kalkyl.spend_ratio, kalkyl.max_scaled_stop)` -> `return stop` in
    `kalkyl_run_stop`: a large tender's run is held at the top tier's $25."""
    kalkyl = load_kalkyl_config()
    stop = kalkyl_run_stop(kalkyl, 200.0)
    assert (stop.tier, stop.usd, stop.above_top) == ("L", 25.0, True)
    assert stop.hard_stop == pytest.approx(200.0 * kalkyl.spend_ratio) == pytest.approx(58.2)
    assert stop.summary()["scaled"] and stop.summary()["hard_stop_usd"] == stop.hard_stop


def test_granskning_s_stop_rises_so_its_cards_and_map_take_at_most_their_share() -> None:
    """Mutation: `return scaled(granskning_stop(config.budgets, prefix_tokens, ESTIMATE), floor, …)` -> `return
    granskning_stop(config.budgets, prefix_tokens, ESTIMATE)` in `granskning_run_stop`: a drawing-heavy tender's page
    cards and map can take the whole tier's stop, and the run halts before its lenses (audit 2, C10)."""
    stop = granskning_run_stop(CONFIG, 400_000, 2_000)
    share = float(BUDGETS["granskning"]["card_map_share"])
    assert (stop.tier, stop.usd) == ("L", 45.0)
    assert stop.hard_stop == pytest.approx(cards_and_map_usd(CONFIG, 400_000, 2_000) / share) and stop.hard_stop > 45
    assert cards_and_map_usd(CONFIG, 400_000, 2_000) <= share * stop.hard_stop + 1e-9  # the lenses keep the rest


def test_the_old_engine_s_stop_rises_to_its_pages_estimate_with_the_margin() -> None:
    """Mutation: `return round(max(float(section["hard_stop"]), floor), 4)` -> `return float(section["hard_stop"])` in
    `baseline_run_stop`: a large tender's run is held at $10."""
    assert baseline_run_stop(BUDGETS, 1000) == pytest.approx(1000 * 0.022 * 1.5) == pytest.approx(33.0)
    assert baseline_run_stop(BUDGETS, 0) == 10.0


def test_a_run_that_lowers_its_tier_never_goes_below_its_floor() -> None:
    """Mutation: `self.hard_stop = min(max(usd, self.floor), self.ceiling)` -> `self.hard_stop = min(usd,
    self.ceiling)` in `Budget.set_stop`: the measured prefix's lower tier cuts the floor."""
    budget = Budget(52.0, wait=True, floor_usd=52.0)
    assert budget.set_stop(20.0) == 52.0 and budget.set_stop(100.0) == 52.0
    budget = Budget(45.0, wait=True, floor_usd=12.0)
    assert budget.set_stop(30.0) == 30.0 and budget.set_stop(5.0) == 12.0


def test_an_engine_s_ceiling_caps_its_floor_but_never_its_tier() -> None:
    """`max_scaled_stop`, unset by default. Mutation: `floor = floor_usd if ceiling_usd is None else min(floor_usd,
    ceiling_usd)` -> `floor = floor_usd` in `stops.scaled`: the ceiling is ignored. Also caught: the old engine's
    `floor = min(floor, ceiling)` -> deleted in `baseline_run_stop`."""
    assert all(BUDGETS[s].get("max_scaled_stop") is None for s in ("granskning", "kalkyl", "baseline"))
    kalkyl = replace(load_kalkyl_config(), max_scaled_stop=40.0)
    assert kalkyl_run_stop(kalkyl, 200.0).hard_stop == 40.0
    assert kalkyl_run_stop(replace(kalkyl, max_scaled_stop=5.0), 200.0).hard_stop == 25.0  # never below the tier's
    capped = copy.deepcopy(CONFIG)
    capped.budgets["granskning"]["max_scaled_stop"] = 60.0
    assert granskning_run_stop(capped, 400_000, 2_000).hard_stop == 60.0
    budgets = copy.deepcopy(BUDGETS)
    budgets["baseline"]["max_scaled_stop"] = 20.0
    assert baseline_run_stop(budgets, 1000) == 20.0


# --- The engines enforce the stop their dry run gives: the stop the exam records as the run's cap ------------------


def test_kalkyl_s_run_spends_under_its_scaled_stop_the_one_its_dry_run_gives(data_root: Path) -> None:
    """Tiers far below the run's spend, a floor above it: the run finishes under its floor. Mutation: `budget =
    Budget(stop.hard_stop, wait=True)` -> `Budget(stop.usd, wait=True)` in `run_dev_tender`: it halts at the tier's
    stop."""
    write_prep(paths.dev_tender_dir("co-001", "t-0001") / "prep")
    write_catalog(data_root)
    kalkyl = KalkylConfig(**{**vars(load_kalkyl_config()), "stops": (Tier("S", 0.0001, 0.0001),), "spend_ratio": 500.0})
    dry = plan("co-001", "t-0001", catalog_name="synthetic", kalkyl=kalkyl)
    expected = kalkyl_run_stop(kalkyl, dry["cost_usd_est_high"]).hard_stop
    assert dry["hard_stop_usd"] == expected > dry["stop_tier_usd"]  # what the exam reads off the dry run
    paid = run_dev_tender(
        "co-001", "t-0001", sdk=fake_sdk(), catalog_name="synthetic", run_id="k-scaled", kalkyl=kalkyl
    )
    assert paid["halted"] == 0 and paid["complete"] and 0 < paid["cost_usd"] < expected
    assert paid["stop"]["hard_stop_usd"] == expected and paid["stop"]["scaled"]


_PREFIX = re.compile(r"estimated prefix: (\d+) tokens")  # as the exam reads the dry run (`exam._PREFIX`, `_TILES`)
_TILES = re.compile(r"^  tiles for page cards: (\d+)$", re.M)


def _tiny_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Granskning's tiers far below the synthetic run's spend, so its floor is the stop in force."""
    real = config_module.load_config

    def load(*args: Any, **kwargs: Any) -> Any:
        config = real(*args, **kwargs)
        config.budgets["granskning_stops"] = [
            {"tier": "S", "max_prefix_tokens": SMALL, "hard_stop": 0.01},
            {"tier": "M", "max_prefix_tokens": 1_000_000, "hard_stop": 0.02},
            {"tier": "L", "max_prefix_tokens": 2_000_000, "hard_stop": 0.03},
        ]
        return config

    monkeypatch.setattr(config_module, "load_config", load)


def test_granskning_s_run_keeps_its_floor_when_the_measured_prefix_lowers_its_tier(
    tender: Path,  # noqa: F811 - the fixture
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The run opens at its floor, the dry run's numbers give the same stop, and the measured prefix's lower tier
    never takes it below. Mutation: `floor_usd=ceiling.floor` -> dropped in `run_command`'s `Budget(...)`: after the
    map, the stop in force falls to the small tier's. Also caught: `stop = replace(stop, floor=client.budget.floor)`
    -> deleted in `run_granskning`: the record says the small tier's stop, and the lens caps are picked for it."""
    _tiny_tiers(monkeypatch)
    assert cli.main(["run", "co-001", "t-0001", "--dry-run"]) == 0
    out = capsys.readouterr().out
    tokens, tiles = int(_PREFIX.search(out).group(1)), int(_TILES.search(out).group(1))
    expected = granskning_run_stop(config_module.load_config(), tokens, tiles)
    assert expected.hard_stop == expected.floor > expected.usd
    assert f"hard stop: tier {expected.tier}, ${expected.hard_stop:.2f} at most" in out
    summary, _, _ = _run(tender, monkeypatch, "t-0001-floor", SMALL)
    assert summary["stop"]["tier"] == "S" and summary["stop"]["tier_usd"] == 0.01  # the measured prefix's tier
    assert summary["stop"]["hard_stop_usd"] == pytest.approx(expected.hard_stop) and summary["stop"]["scaled"]
    assert summary["budget"]["refused_calls"] == 0 and not summary["budget"]["lenses_not_run"]


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun isn't installed")
def test_the_old_engine_s_dev_run_hands_its_scaled_stop_to_the_preload(data_root: Path, tmp_path: Path) -> None:
    """Mutation: `cap = run_stop(prep_pages(paths.dev_tender_dir(company_id, tender_id, root)))` -> `cap =
    float(budget["hard_stop"])` in `run_dev`: the run is held at $10 whatever its size."""
    tender_dir = dev_tender(data_root)
    (tender_dir / "prep").mkdir()
    (tender_dir / "prep" / "summary.json").write_text(json.dumps({"pages_by_kind": {"pdf": 900, "xlsx": 100}}))
    assert baseline.prep_pages(tender_dir) == 1000 and baseline.run_stop(1000) == pytest.approx(33.0)
    with pytest.raises(baseline.BaselineError, match=r"\$33\.00 hard stop"):  # a lower stop is allowed, never higher
        baseline.run_dev("co-001", "t-0001", dry_run=True, hard_stop=34.0, app=tmp_path, code={}, stop_requested=bool)
    result = baseline.run_dev(
        "co-001",
        "t-0001",
        dry_run=False,
        app=fake_app(tmp_path, FINISHING),
        app_ref="HEAD",
        code={"harness_sha": "x"},
        stop_requested=lambda _: False,
        basis="app-defaults",
        api_key=lambda: "test-key-never-sent",
        now=NOW,
    )
    record = paths.baseline_dir("co-001", "t-0001") / result.run_id / "record"
    assert json.loads((record / "run.json").read_text())["hard_stop_usd"] == pytest.approx(33.0)
    start = json.loads((record / "calls.jsonl").read_text().splitlines()[0])
    assert start["event"] == "start" and start["hard_stop_usd"] == pytest.approx(33.0)  # what the preload enforces
