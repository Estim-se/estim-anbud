"""The $0 pass's scaled stops: what the exam would cost if each tender's stop were scaled so that no engine is cut off
by its cap. A diagnostic for a cap decision, from `config/exam_checks.toml`; no stop reads it.

Per tender, never below its tier's stop: the old engine at its estimate with the margin; Kalkyl at its dry run's high
estimate times the largest spend ratio; Granskning at its page cards and map over `card_map_share`, so the lenses keep
the rest. Synthetic store and fake engines (test_exam_size_checks); no model call.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from test_exam import LOCAL_NOW, NOW, FakeEngines, register
from test_exam_size_checks import COSTS, HIGHS, PAGES, ROOT, TILES, _free_pass

from anbud.config import CONFIG_DIR
from anbud.evals import exam

LINE = (
    "scaled stops, if no engine were cut off by its cap (a diagnostic; config/exam_checks.toml), over the tenders each "
    "engine runs on: worst case $309.72 (granskning $127.02, kalkyl $93.30, baseline $89.40); estimate $200.11 "
    "(granskning $45.46, kalkyl $90.30, baseline $64.35); stops that would rise: granskning 2 of 4, kalkyl 2 of 4, "
    "baseline 2 of 4; tenders per largest stop: granskning <=$60 3, <=$100 1, <=$150 0, above $150 0; kalkyl <=$60 4, "
    "<=$100 0, <=$150 0, above $150 0; baseline <=$60 4, <=$100 0, <=$150 0, above $150 0"
)


def _scaled(lines: list[str]) -> str:
    (line,) = [x for x in lines if x.startswith("scaled stops")]
    return line


def test_the_free_pass_prints_the_exam_with_stops_scaled_to_each_tender(data_root: Path) -> None:
    """At the fake's 10,000-token prefix Granskning opens at S ($20); 400 tiles ($12) plus the map ($0.76) need $25.51
    for the cards and map to take half, 1,000 tiles $61.51. Kalkyl's $100 and $200 highs need $29.10 and $58.20 (0.291);
    $1 stays at S's $3. The old engine's 400 and 500 pages need $13.20 and $16.50 a run; 50 stays at $10. Mutations:
    `run_stop = raw * margin` (no floor at the stop), `min` for `max` over the spend ratios, or Granskning's estimate
    without its cards and map, each make it fail."""
    lines = _free_pass(data_root)
    assert _scaled(lines) == LINE
    size = next(i for i, x in enumerate(lines) if x.startswith("size checks"))
    assert lines[size + 1] == LINE  # one place to read: right after the size checks, in the one total


def test_the_line_depends_on_no_fact_of_one_split(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The template from the paid total (test_exam_total_one_split): prospective deadlines past or open, the same."""
    from test_exam_total_one_split import OPEN, PAST, _store

    found: dict[str, str] = {}
    for name, deadlines in (("past", PAST), ("open", OPEN)):
        root = tmp_path / name
        root.mkdir()
        monkeypatch.setenv("ANBUD_DATA_ROOT", str(root))
        _store(root, deadlines)
        register(root)
        said: list[str] = []
        runner = FakeEngines(COSTS, tiles=TILES, highs=HIGHS, pages=PAGES)
        exam.run_both("co-002", runner=runner, now=NOW, local_now=LOCAL_NOW, say=said.append)
        found[name] = _scaled(said)
    assert found["past"] == found["open"]


def test_the_line_follows_the_config(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: fixed bands in `_scaled_totals` instead of `[scaled] stop_bands` makes it fail."""
    checks = {
        "granskning": {"card_usd_per_tile": 0.03, "card_map_share": 0.25},
        "kalkyl": {"spend_ratios": [0.1, 0.5]},
        "scaled": {"stop_bands": [20, 40]},
    }
    monkeypatch.setattr(exam, "exam_checks", lambda config_dir=CONFIG_DIR: checks)
    line = _scaled(_free_pass(data_root))
    assert "kalkyl $156.00" in line  # $3 + $50 + $100 + $3 at the largest ratio, 0.5
    assert "kalkyl <=$20 2, <=$40 0, above $40 2" in line
    # At a quarter, the map alone ($0.76, $3.04 over 0.25) and the unknown tiles keep S's $20; 400 and 1,000 tiles rise.
    assert "granskning 2 of 4" in line and "granskning <=$20 2, <=$40 0, above $40 2" in line


def test_a_scaled_stop_is_never_below_the_tiers_and_an_unknown_size_keeps_it() -> None:
    assert exam._scaled_kalkyl(25.0, 10.0, 1.5) == {"stop": 25.0, "estimate": 15.0, "rises": False, "largest": 25.0}
    assert exam._scaled_kalkyl(25.0, None, 1.5)["stop"] == 25.0
    assert exam._scaled_granskning(20.0, None, None) == {
        "stop": 20.0,
        "estimate": 20.0,
        "rises": False,
        "largest": 20.0,
    }
    assert exam._scaled_granskning(0.0, None, None)["stop"] == 0.0  # a prefix-budget stop runs nothing: no rise
    assert exam._scaled_granskning(20.0, 1.0, 4.0) == {"stop": 20.0, "estimate": 4.0, "rises": False, "largest": 20.0}


def test_nothing_goes_into_the_guarded_budgets() -> None:
    """The diagnostics stay in exam_checks.toml; budgets.toml holds only the scaled stops' own parameters
    (`anbud.stops`, the draft PR that needs the founder's yes)."""
    budgets = (CONFIG_DIR / "budgets.toml").read_text()
    assert "stop_bands" not in budgets and "spend_ratios" not in budgets
    assert exam.exam_checks()["scaled"]["stop_bands"] == [60, 100, 150]


def test_the_wrapper_keeps_the_line_among_its_status_lines() -> None:
    script = (ROOT / "scripts" / "exam-run.sh").read_text()
    (pattern,) = re.findall(r"^STATUS_LINES='(.+)'$", script, re.M)
    assert re.match(pattern, "scaled stops, if no engine were cut off by its cap ...")
