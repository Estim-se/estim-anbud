"""Page cards shared between pipelines: the card store, reuse of an earlier run's step cache, and Kalkyl's use of the
cards' tile_text blocks. Synthetic tiles, a fake SDK, $0."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from kalkyl_synthetic import CARD_TEXT, add_tile, fake_sdk, message, text, write_catalog, write_prep

from anbud import paths
from anbud.config import load_config
from anbud.granskning.card_store import CARD_DIR, load_or_read_cards, plan_cards
from anbud.granskning.page_cards import read_cards
from anbud.kalkyl.run import card_source_runs, plan, run_dev_tender
from anbud.model_client import Budget, ModelClient
from anbud.prep.tender import IntakeStop
from anbud.store import RunStore


class NoCalls:
    def count_tokens(self, **_: Any) -> Any:
        raise AssertionError("no model call expected")

    def stream(self, **_: Any) -> Any:
        raise AssertionError("no model call expected")


def _client(sdk: Any, store: RunStore, run_id: str) -> ModelClient:
    return ModelClient(load_config(), sdk, Budget(1.0), run_id=run_id, company_id="co-001", store=store)


@pytest.fixture
def setup(data_root: Path):
    store = RunStore("co-001")
    tender = paths.dev_tender_dir("co-001", "t-0001")
    prep = write_prep(tender / "prep")
    tile = add_tile(prep, store)
    write_catalog(data_root)
    return store, tender, tile


def test_a_card_is_read_once_then_served_from_the_store(setup) -> None:
    store, _, tile = setup
    sdk = fake_sdk()
    first = load_or_read_cards(_client(sdk, store, "kalkyl-a"), store, [tile])
    assert first.sources == {"read": 1} and len(sdk.messages.sent) == 1
    assert [b.kind for b in first.cards.blocks] == ["tile_text", "tile_text"]
    assert first.cards.blocks[1].text == "Uttag 3 st i förråd" and first.cards.blocks[0].id == f"{tile.id}-1"
    assert list((store.dir / CARD_DIR).rglob("*.json"))
    again = load_or_read_cards(_client(SimpleNamespace(messages=NoCalls()), store, "kalkyl-b"), store, [tile])
    assert again.sources == {"stored": 1} and again.cards.blocks == first.cards.blocks


def test_cards_an_earlier_run_read_cost_nothing_and_that_run_is_left_untouched(setup) -> None:
    store, _, tile = setup
    granskning = _client(fake_sdk(), store, "t-0001-20260101T000000Z")
    read_cards(granskning, store, [tile])  # as `anbud run` does: into its own step cache only
    events = (store.run_dir("t-0001-20260101T000000Z") / "events.jsonl").read_bytes()
    assert card_source_runs(store, "t-0001") == ["t-0001-20260101T000000Z"]
    offline = ModelClient(load_config(), None, Budget(0.0), run_id="plan", company_id="co-001")
    assert plan_cards(offline, store, [tile], ["t-0001-20260101T000000Z"]) == {"earlier_run": 1}
    kalkyl = _client(SimpleNamespace(messages=NoCalls()), store, "kalkyl-c")
    loaded = load_or_read_cards(kalkyl, store, [tile], ["t-0001-20260101T000000Z"])
    assert loaded.sources == {"earlier_run": 1} and loaded.cards.cost_usd == 0
    assert loaded.cards.blocks[1].text == "Uttag 3 st i förråd"
    assert (store.run_dir("t-0001-20260101T000000Z") / "events.jsonl").read_bytes() == events
    assert plan_cards(offline, store, [tile]) == {"stored": 1}  # and now it is in the card store


def test_another_image_is_another_card(setup) -> None:
    store, tender, tile = setup
    load_or_read_cards(_client(fake_sdk(), store, "kalkyl-d"), store, [tile])
    other = add_tile(tender / "prep", store, b"another synthetic image")
    offline = ModelClient(load_config(), None, Budget(0.0), run_id="plan", company_id="co-001")
    assert plan_cards(offline, store, [other]) == {"read": 1}


def test_kalkyl_reads_the_cards_tile_text_and_accounts_for_its_quantity_lines(setup) -> None:
    store, tender, tile = setup
    summary = run_dev_tender("co-001", "t-0001", sdk=fake_sdk(), catalog_name="synthetic", run_id="kalkyl-e")
    assert summary["tiles"] == 1 and summary["cards"] == {"read": 1} and summary["tiles_unread"] == 0
    result = json.loads((tender / "kalkyl" / "kalkyl-e" / "result.json").read_text())
    assert f"{tile.id}-2" in {u["block_id"] for u in result["uncovered"]}  # a card's quantity line is never dropped
    replay = run_dev_tender(
        "co-001", "t-0001", sdk=None, catalog_name="synthetic", run_id="kalkyl-e2", base_run="kalkyl-e"
    )  # a finished run is frozen, so its replay is a rebuild with its own run id
    assert replay["cards"] == {"stored": 1} and replay["complete"]


def test_a_security_marking_on_a_card_stops_the_run(setup) -> None:
    sdk = fake_sdk(lambda params: message([text("HEMLIG")], stop_reason="end_turn"))
    with pytest.raises(IntakeStop):
        run_dev_tender("co-001", "t-0001", sdk=sdk, catalog_name="synthetic", run_id="kalkyl-f")


def test_the_dry_run_counts_cards_and_writes_nothing(setup) -> None:
    store, _, _ = setup
    before = sorted(p.relative_to(store.dir) for p in store.dir.rglob("*"))
    counts = plan("co-001", "t-0001", catalog_name="synthetic")
    assert counts["tiles"] == 1 and counts["cards_to_read"] == 1 and counts["cost_usd_est_low"] > 0
    assert sorted(p.relative_to(store.dir) for p in store.dir.rglob("*")) == before
    assert CARD_TEXT  # the fake card is synthetic
