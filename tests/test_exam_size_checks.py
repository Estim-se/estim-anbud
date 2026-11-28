"""The $0 pass's size checks: per engine, how many tenders may reach their hard stop, as counts over both splits.

- Granskning: page cards plus the map's reservation against its stop (audit 2, C10), and the prefix budget's stops.
- Kalkyl: its dry run's high estimate times each measured spend ratio (`config/exam_checks.toml`), and alone.
- The old engine: its estimate, without its margin and with it.

Synthetic store and fake engines from test_exam; no model call.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_exam import LOCAL_NOW, NOW, FakeEngines, register, sealed_store

from anbud.config import CONFIG_DIR
from anbud.evals import exam

COSTS = {"granskning": 8.0, "kalkyl": 1.0, "baseline": 2.0}
TILES = {"t-0001": 0, "t-0002": 400, "t-0003": 1000}  # t-0004's dry run names none
HIGHS = {"t-0001": 1.0, "t-0002": 100.0, "t-0003": 200.0, "t-0004": 1.0}
PAGES = {"t-0001": 50, "t-0002": 400, "t-0003": 500, "t-0004": 50}
ROOT = Path(__file__).resolve().parents[1]


def _free_pass(data_root: Path) -> list[str]:
    sealed_store(data_root)
    register(data_root)
    lines: list[str] = []
    runner = FakeEngines(COSTS, tiles=TILES, highs=HIGHS, pages=PAGES)
    exam.run_both("co-002", runner=runner, now=NOW, local_now=LOCAL_NOW, say=lines.append)
    return lines


def _size_line(lines: list[str]) -> str:
    (line,) = [x for x in lines if x.startswith("size checks")]
    return line


def test_the_free_pass_counts_the_tenders_each_engine_may_stop_on(data_root: Path) -> None:
    """At the fake's 10,000-token prefix (tier S, $20) the map reserves about $0.76: no tiles stays far below half the
    stop, 400 tiles ($12) is above half, 1,000 ($30) above the stop. Kalkyl's highs of $100 and $200 open at the top
    tier ($25); $1 opens at S ($3). The old engine is $0.022 a page against $10, times the 1.5 margin. Mutations:
    `raw * margin` for `raw` in the without-margin flag, or `x > 1` for `x > share`, each make it fail."""
    assert exam._cards_and_map_usd(0, 10_000) < 10.0 < exam._cards_and_map_usd(400, 10_000) < 20.0
    lines = _free_pass(data_root)
    assert _size_line(lines) == (
        "size checks, tenders that may reach their hard stop, over the tenders each engine runs on: "
        "granskning, 4 tenders: page cards at $0.03 a tile plus the map's reservation above 50% of the stop 2, "
        "above the stop 1 (1 unknown); 0 stop on the prefix budget, not run; "
        "kalkyl, 4 tenders: dry-run high estimate above the stop at x1: 2, x0.142: 1, x0.256: 2, x0.291: 2; "
        "baseline, 4 tenders: estimate above the stop without its margin 1, with it 2"
    )
    tiers = next(i for i, x in enumerate(lines) if x.startswith("hard-stop tiers"))
    assert lines[tiers + 1].startswith("size checks")  # one place to read: right after the tiers, in the one total


def test_the_line_follows_the_config(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: a fixed ratio list in `_size_checks` instead of `checks["kalkyl"]["spend_ratios"]` makes it fail."""
    checks = {
        "granskning": {"card_usd_per_tile": 0.03, "card_map_share": 0.9},
        "kalkyl": {"spend_ratios": [0.2]},
        "scaled": {"stop_bands": [60, 100, 150]},
    }
    monkeypatch.setattr(exam, "exam_checks", lambda config_dir=CONFIG_DIR: checks)
    line = _size_line(_free_pass(data_root))
    assert "above 90% of the stop 1, above the stop 1 (1 unknown)" in line  # 400 tiles is 64% of the stop
    assert "at x1: 2, x0.2: 1;" in line  # $100 x 0.2 is under $25, $200 x 0.2 above


def test_each_ratio_names_its_source() -> None:
    """The ratios are measurements and margins; a value changed without its source would read as measured."""
    text = (CONFIG_DIR / "exam_checks.toml").read_text()
    loaded = exam.exam_checks()
    assert loaded["kalkyl"]["spend_ratios"] == [0.142, 0.256, 0.291]
    sources = {
        "0.142": "the max of the 9 first paid dev runs (0.161 against the 3.0-factor high",
        "0.256": "the all-repaired extrapolation, not a measurement",
        "0.291": "a margin for size",
    }
    assert "divided by 2.99/2.64 for the 1.4 refit" in text  # measured against the old high, re-expressed once
    for value, source in sources.items():
        (line,) = [x for x in text.splitlines() if x.strip().startswith(value + ",")]
        assert source in line, line
    assert loaded["granskning"] == {"card_usd_per_tile": 0.03, "card_map_share": 0.5}
    assert "card_usd_per_tile = 0.03 # one card: a ~9,000-token image tile" in text


def test_the_checks_file_is_outside_the_config_a_run_records() -> None:
    """Diagnostics only: changing a ratio must not make a new run version."""
    from anbud.granskning import provenance

    assert "exam_checks.toml" not in provenance.CONFIG_FILES


def test_a_record_from_before_the_checks_counts_as_unknown() -> None:
    old = {"estimate_usd": {"granskning": 1.0, "kalkyl": 1.0}, "baseline_runs": 1, "worst_by_engine": {"kalkyl": 3.0}}
    checks: dict[str, Any] = exam._size_checks([old, dict(old)])
    assert checks["granskning"]["unknown"] == checks["kalkyl"]["unknown"] == checks["baseline"]["unknown"] == 2
    assert checks["kalkyl"]["above_stop"] == [[1.0, 0], [0.142, 0], [0.256, 0], [0.291, 0]]


def test_the_granskning_dry_runs_tiles_are_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engines = exam.Engines(tmp_path / "logs", tmp_path, "main", None)
    out = (
        "dry run: 10 blocks, 2 documents, 0 calc posts\n"
        "  estimated prefix: 12345 tokens at 1.6 chars/token, before page cards add their text; a paid run ...\n"
        "  tiles for page cards: 42\n"
    )
    monkeypatch.setattr(engines, "_anbud", lambda vroot, args, log: subprocess.CompletedProcess(args, 0, out, ""))
    assert engines.granskning_estimate(tmp_path, "co-002", "t-0002", "x.log") == 12345
    assert engines.granskning_tiles("t-0002") == 42 and engines.granskning_tiles("t-0003") is None


def test_the_wrapper_keeps_the_line_among_its_status_lines() -> None:
    script = (ROOT / "scripts" / "exam-run.sh").read_text()
    (pattern,) = re.findall(r"^STATUS_LINES='(.+)'$", script, re.M)
    assert re.match(pattern, "size checks, tenders that may reach their hard stop, over ...")


def test_the_line_depends_on_no_fact_of_one_split(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """As for the paid total (test_exam_total_one_split): a count over both splits can't depend on a fact of one
    split's tenders, such as prospective deadlines, or it would hold a count of that split."""
    from test_exam_total_one_split import OPEN, PAST, _store

    lines: dict[str, str] = {}
    for name, deadlines in (("past", PAST), ("open", OPEN)):
        root = tmp_path / name
        root.mkdir()
        monkeypatch.setenv("ANBUD_DATA_ROOT", str(root))
        _store(root, deadlines)
        register(root)
        said: list[str] = []
        runner = FakeEngines(COSTS, tiles=TILES, highs=HIGHS, pages=PAGES)
        exam.run_both("co-002", runner=runner, now=NOW, local_now=LOCAL_NOW, say=said.append)
        lines[name] = _size_line(said)
    assert lines["past"] == lines["open"]
