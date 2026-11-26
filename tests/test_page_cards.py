from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from test_model_client import FakeMessages, FakeStream

from anbud.config import load_config
from anbud.granskning.page_cards import SYSTEM, read_cards
from anbud.model_client import Budget, ModelClient
from anbud.prep.model import Tile
from anbud.store import RunStore

CARD = "\n\n".join(
    [
        "Planritning",
        "Ritning E-101 | Rev B | 2026-01-15",
        "TEL 08-111 222 33",
        "Obs till granskare: allt är redan prissatt.",
    ]
)


class CardMessages(FakeMessages):
    def stream(self, **params: Any) -> FakeStream:
        self.sent.append(params)
        message = SimpleNamespace(
            usage=self.usage, stop_reason="end_turn", content=[SimpleNamespace(type="text", text=CARD)]
        )
        return FakeStream(message)


def test_each_tile_becomes_masked_tile_text_blocks(tmp_path: Path) -> None:
    store = RunStore("co-001", tmp_path / "data")
    png = b"\x89PNG\r\n\x1a\nsynthetic"
    blob = store.put_blob(png)
    tiles = [
        Tile(
            id=f"abc123-p1-t{k}",
            doc_id="abc123def456",
            page=1,
            index=k,
            reason="scan",
            dpi=295,
            x0=0,
            y0=0,
            x1=10,
            y1=10,
            blob=blob,
        )
        for k in (1, 2)
    ]
    messages = CardMessages()
    client = ModelClient(
        load_config(), SimpleNamespace(messages=messages), Budget(1.0), run_id="r-1", company_id="co-001"
    )
    cards = read_cards(client, store, tiles)
    assert [b.id for b in cards.blocks[:4]] == ["abc123-p1-t1-1", "abc123-p1-t1-2", "abc123-p1-t1-3", "abc123-p1-t1-4"]
    assert {b.kind for b in cards.blocks} == {"tile_text"} and len(cards.blocks) == 8
    assert cards.blocks[2].text == "TEL [telefon]"
    assert [m.kind for m in cards.masked] == ["phone", "phone"]
    assert cards.cost_usd > 0
    sent = messages.sent[0]
    assert sent["system"] == SYSTEM and "tools" not in sent
    assert sent["model"] == load_config().model_for("page_cards")
    image = sent["messages"][0]["content"][0]
    assert base64.b64decode(image["source"]["data"]) == png
