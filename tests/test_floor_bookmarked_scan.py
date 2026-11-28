"""A page card never raises the prefix floor after the $0 check before the cards. A scanned PDF that prep gave
bookmarks (outline entries with no block) and only a page-number stamp as text: before its card, its text is smaller
than its outline; once the card's text joins, the outline is the smaller, so the least the document comes to rises to
it. The check before the cards counts a document with cards to come at its outline, so a budget that passes it never
stops in `fit_prefix` after paying for the card. Synthetic documents, scripted fake SDK, $0.

The test names the one-line mutation it catches; it was made, seen to fail the test, and reverted."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from granskning_fakes import DOC_RIT, ScriptedSDK, png_bytes, reply, write_prep
from test_granskning_pipeline import _calc, _catalog, clean_tree  # noqa: F401 - the fixture
from test_runguard import ledger_with

from anbud import cli, model_client
from anbud import config as config_module
from anbud.evals import split_heldout as sh
from anbud.granskning.budget import PrefixBudget, plan_prefix
from anbud.granskning.calc import load_calc
from anbud.selection import load_selected
from anbud.store import RunStore

# An invented dense page of text read off the scan.
CARD = "\n\n".join(f"Påhittat stycke {n}: " + "text på en inskannad sida " * 20 for n in range(12))


def _bookmarked_scan(prep: Path) -> None:
    """The tender's tiled document becomes a scan with a page-number stamp as its only text and 40 bookmarks."""
    with (prep / "blocks.jsonl").open("a") as f:
        stamp = {"id": "cccccc-000001", "doc_id": DOC_RIT, "kind": "paragraph", "text": "1", "page": 1, "line": 1}
        f.write(json.dumps(stamp) + "\n")
    outline = json.loads((prep / "outline.json").read_text())
    outline += [
        {"doc_id": DOC_RIT, "level": 1, "title": f"Avsnitt {n} med en ganska lang rubrik om villkor", "page": 1}
        for n in range(1, 41)
    ]
    (prep / "outline.json").write_text(json.dumps(outline, ensure_ascii=False))
    docs = json.loads((prep / "documents.json").read_text())
    for d in docs:
        if d["doc_id"] == DOC_RIT:
            d["blocks"] = 1
    (prep / "documents.json").write_text(json.dumps(docs))
    tiles = json.loads((prep / "tiles.json").read_text())
    tiles[0]["reason"] = "scan"
    (prep / "tiles.json").write_text(json.dumps(tiles))


@pytest.fixture
def tender(data_root: Path) -> Path:
    ledger_with(data_root, t_0001=sh.DEV)
    blob = RunStore("co-001").put_blob(png_bytes())
    base = data_root / "co-001" / "dev" / "t-0001"
    write_prep(base / "prep", tile_blob=blob)
    _bookmarked_scan(base / "prep")
    _calc(base / "calc")
    _catalog(data_root)
    return base


def test_a_run_that_passes_the_check_before_the_cards_never_stops_after_paying_for_one(
    tender: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The budget is the floor the check before the cards computes: exactly what it lets through. Mutation:
    `outline.get(d, 0) if d in tiled else min(sizes[d], outline.get(d, 0))` -> `min(sizes[d], outline.get(d, 0))` in
    `plan_prefix`: the scan counts at its stamp before the card, the floor rises after it, and the paid run stops."""
    corpus, _ = load_selected(tender / "prep")
    calc = load_calc(tender / "calc")
    tokens = plan_prefix(corpus, calc, PrefixBudget(10**9, 0.1), corpus.tiles.values()).floor
    assert not plan_prefix(corpus, calc, PrefixBudget(tokens, 0.1), corpus.tiles.values()).stops

    real = config_module.load_config

    def load_config(*args: object, **kwargs: object) -> object:
        config = real(*args, **kwargs)
        config.budgets["prefix"] = {"budget_tokens": tokens, "boundary_share": 0.1}
        return config

    monkeypatch.setattr(config_module, "load_config", load_config)
    sdk = ScriptedSDK(lambda params: reply(CARD))
    monkeypatch.setattr(model_client, "keychain_key", lambda **_: "sk-test")
    monkeypatch.setattr(model_client, "make_sdk_client", lambda config, key: sdk)
    code = cli.main(["run", "co-001", "t-0001", "--run-id", "t-0001-bookmarked"])
    err = capsys.readouterr().err
    assert sdk.sent, "the check before the cards passed, so the card was read"
    assert "stopped: the prefix's fixed part" not in err and code == 0
