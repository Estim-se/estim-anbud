"""`anbud regress`, on an invented tender in a temporary data root."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import get_args

import pytest

from anbud import cli, paths
from anbud.evals import split_heldout as sh
from anbud.granskning.contracts import fingerprint
from anbud.granskning.items import Item, Placement
from anbud.granskning.regress import RegressionSet, check, entry_from_item, matches, regress, siblings
from anbud.models import Citation, Flag, MoneyBasis, Verdict

COMPANY, TENDER = "co-001", "t-0001"
SOURCE, RUN = "t-0001-src", "t-0001-new"
SECRET = "Påhittad kabelstege i källaren"


def _item(claim_type: str, blocks: list[str], placement: str = "shown", post: str | None = None) -> Item:
    flag = Flag(
        lens_id="lens",
        claim_type=claim_type,
        claim=SECRET,
        citations=[Citation(block_id=b, quote="citat") for b in blocks],
        money_basis=MoneyBasis(kind="missing_post", post_ref=post),
    )
    verdict = Verdict(
        fingerprint=fingerprint(flag),
        verdict="refuted" if placement == "refuted" else "confirmed",
        electrical_scope=True,
        covered_by_calc=False,
        calc_post="calc-s1-r9",
        would_be_wrong_if="x",
    )
    return Item(flag=flag, fingerprint=fingerprint(flag), lenses=["lens"], verdict=verdict, placement=placement)


def _write_run(tender: Path, run_id: str, items: list[Item], frozen: bool = False) -> Path:
    run = tender / "runs" / run_id
    run.mkdir(parents=True)
    (run / "items.json").write_text(json.dumps([i.model_dump(mode="json") for i in items]))
    if frozen:
        os.chmod(run / "items.json", stat.S_IRUSR)
        os.chmod(run, stat.S_IRUSR | stat.S_IXUSR)
    return run


SOURCE_ITEMS = [
    _item("unpriced_requirement", ["aa-1", "aa-2"], "capped", post="calc-s1-r3"),
    _item("grid_interface", ["bb-1"], "capped"),
    _item("missing_document", ["cc-1"], "shown"),
]


@pytest.fixture
def tender(data_root: Path) -> Path:
    sh.append_events(paths.ledger_path(COMPANY), [sh._assign(TENDER, sh.DEV, "test", ())])
    folder = paths.dev_tender_dir(COMPANY, TENDER)
    entries = [entry_from_item(item, SOURCE, n, f"Entry {n}.") for n, item in enumerate(SOURCE_ITEMS)]
    regression = RegressionSet(company_id=COMPANY, tender_id=TENDER, entries=entries)
    (folder / "labels").mkdir(parents=True)
    (folder / "labels" / "regression.json").write_text(regression.model_dump_json())
    return folder


def _set(tender: Path) -> RegressionSet:
    return RegressionSet.load(tender / "labels" / "regression.json")


def test_entries_keep_blocks_and_calc_posts() -> None:
    entry = entry_from_item(SOURCE_ITEMS[0], SOURCE, 0, "p")
    assert entry.block_ids == ["aa-1", "aa-2"]
    assert entry.calc_post_ids == ["calc-s1-r3", "calc-s1-r9"]
    assert entry.fingerprint == SOURCE_ITEMS[0].fingerprint and entry.flag_number == 0


def test_siblings_share_a_taxonomy_item() -> None:
    assert siblings("contract_vite", "contract_payment")  # both item 1
    assert siblings("missing_document", "document_conflict")  # both item 14
    assert not siblings("unpriced_requirement", "missing_document")  # 15 and 14
    assert not siblings("unpriced_requirement", "not_a_type")


def test_a_match_needs_block_overlap_and_a_related_claim_type(tender: Path) -> None:
    entry = _set(tender).entries[2]  # missing_document on cc-1
    assert matches(entry, _item("document_conflict", ["cc-1", "zz"]))
    assert not matches(entry, _item("document_conflict", ["zz"]))
    assert not matches(entry, _item("unpriced_requirement", ["cc-1"]))


def test_only_shown_items_surface_an_entry(tender: Path) -> None:
    result = check(_set(tender), SOURCE_ITEMS, SOURCE)
    assert (result["surfaced"], result["found"]) == (1, 3)
    assert not result["passed"]
    assert result["not_surfaced"] == [0, 1] and result["not_found"] == []


def test_generic_logged_and_refuted_items_dont_surface(tender: Path) -> None:
    items = [
        _item("contract_vite", ["aa-1"]),  # overlap, unrelated claim type: not caught at all
        _item("grid_interface", ["bb-1"], "refuted"),  # refuted: not found
        _item("document_conflict", ["cc-1"], "generic"),  # sibling, found, but generic isn't surfaced
    ]
    result = check(_set(tender), items, RUN)
    assert result["not_found"] == [0, 1]
    assert result["not_surfaced"] == [0, 1, 2]
    assert (result["surfaced"], result["found"]) == (0, 1)
    assert result["results"][1]["items"][0]["placement"] == "refuted"


def test_every_entry_surfaced_passes(tender: Path) -> None:
    items = [_item(i.flag.claim_type, [c.block_id for c in i.flag.citations], "shown") for i in SOURCE_ITEMS]
    result = check(_set(tender), items, RUN)
    assert result["passed"] and result["surfaced"] == 3


@pytest.mark.skipif("shown_unpriced" not in get_args(Placement), reason="B's shown_unpriced placement hasn't landed")
def test_shown_unpriced_surfaces_an_entry(tender: Path) -> None:
    item = _item("unpriced_requirement", ["aa-1"], "shown_unpriced")
    assert check(_set(tender), [item], RUN)["results"][0]["surfaced"]


def test_result_goes_in_the_run_folder_when_writable(tender: Path) -> None:
    run = _write_run(tender, RUN, SOURCE_ITEMS)
    result, out = regress(COMPANY, tender, RUN)
    assert out == run / "regress.json"
    assert json.loads(out.read_text())["found"] == result["found"] == 3
    assert result["surfaced"] == 1


def test_a_frozen_run_is_never_written(tender: Path) -> None:
    run = _write_run(tender, SOURCE, SOURCE_ITEMS, frozen=True)
    try:
        _, out = regress(COMPANY, tender, SOURCE)
        assert out == tender / "labels" / f"regress-{SOURCE}.json"
        assert sorted(p.name for p in run.iterdir()) == ["items.json"]
    finally:
        os.chmod(run, stat.S_IRWXU)


def test_cli_prints_counts_and_indexes_only(tender: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_run(tender, RUN, [SOURCE_ITEMS[0], _item("missing_document", ["cc-1"], "refuted")])
    code = cli.main(["regress", COMPANY, TENDER, RUN])
    out = capsys.readouterr().out
    assert code == 1
    assert "surfaced: 0 of 3 (FAIL)" in out and "found: 1 of 3" in out
    assert "not surfaced: [0, 1, 2]" in out and "not found: [1, 2]" in out
    assert SECRET not in out and "Entry" not in out and "aa-1" not in out


def test_cli_exit_code_follows_the_gate(tender: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_run(tender, RUN, SOURCE_ITEMS)  # all found, one surfaced
    assert cli.main(["regress", COMPANY, TENDER, RUN]) == 1
    shown = [_item(i.flag.claim_type, [c.block_id for c in i.flag.citations], "shown") for i in SOURCE_ITEMS]
    _write_run(tender, "t-0001-shown", shown)
    assert cli.main(["regress", COMPANY, TENDER, "t-0001-shown"]) == 0


def test_cli_refuses_tenders_outside_dev_and_bad_inputs(tender: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["regress", COMPANY, "t-0002", RUN]) == 3  # not in the split ledger
    assert cli.main(["regress", COMPANY, TENDER, "../escape"]) == 3
    assert cli.main(["regress", COMPANY, TENDER, "no-such-run"]) == 3
    _write_run(tender, RUN, SOURCE_ITEMS)
    (tender / "labels" / "regression.json").write_text('{"company_id": "co-002", "tender_id": "t-0001", "entries": []}')
    assert cli.main(["regress", COMPANY, TENDER, RUN]) == 3
    assert SECRET not in capsys.readouterr().err


def test_runs_written_by_older_code_still_load(tender: Path) -> None:
    old = [
        i.model_dump(mode="json") | {"kronor": {"priced": True, "basis": "text from an older version"}}
        for i in SOURCE_ITEMS
    ]
    run = tender / "runs" / RUN
    run.mkdir(parents=True)
    (run / "items.json").write_text(json.dumps(old))
    result, _ = regress(COMPANY, tender, RUN)
    assert result["found"] == 3
