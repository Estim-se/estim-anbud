"""What the model is sent for a fixed input, pinned. A frozen run is rebuilt at $0 from its step cache, keyed by each
request's bytes: any change to a request it made (a prompt, a tool, a schema, how the calculation is read) and the
rebuild misses the cache and exits replay-incomplete. That is how the dev tender's 26 Nov run stopped replaying: four
intended changes after it each moved its map request (the contract-formula input names in the brief; the
catalog_search tool and its schema field; a new claim type in the brief; subtotal rows left out of the calculation).

These pins catch the next such change in CI, on the synthetic tender, before it merges. When a pin moves:
- if the change is not meant to alter requests, it is a bug: find it;
- if it is, update the pin, and say in the PR that runs frozen before it no longer rebuild at $0, with the dev
  tender's request table.

The verifier's requests aren't pinned: which flags it samples is seeded by the run id. Page-card requests aren't
either: their system prompt and tile images are pinned by the page-card tests. Synthetic tender, scripted SDK, $0.

Each test names the one-line mutation it catches; each was made, seen to fail the test, and reverted."""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from pathlib import Path
from typing import Any

import pytest
from granskning_fakes import ScriptedSDK, first_text
from test_granskning_pipeline import clean_tree, script, tender  # noqa: F401 - fixtures

from anbud import cli, model_client
from anbud.granskning.page_cards import SYSTEM as CARDS_SYSTEM
from anbud.store import canonical

# The first request of each class on the synthetic tender, sha256 of its canonical JSON, first 12 hex digits.
MAP_PINS = {
    "system": "35881da13a43",  # every Opus role's system blocks: the rules, the brief, the corpus, the calculation
    "tools": "ed170310f32b",  # every Opus role's tools array
    "map_message": "350107e7a299",
    "params": "c2f492fc4d33",  # model, max_tokens, thinking, tool_choice and the rest
}
LENS_PINS = {
    "contract_terms": "76546f3f483d",
    "document_conflicts": "dd25e747d09e",
    "generic_scope": "9b7ddfa57d00",
    "missing_documents": "1249af1b38f5",
    "occupied_building": "0db1b38c3a2c",
    "quantities_hours": "f59dbc319528",
    "regulation": "8eb7d932e15c",
    "trade_boundaries": "4d0b577d4674",
}
MOVED = (
    "these requests changed, so runs frozen before this change no longer rebuild at $0. If that is intended, update "
    "the pins and say so in the PR, with the dev tender's request table; if not, find what moved them"
)


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()[:12]


def _fixed_bytes(xlsx: Path) -> None:
    """The synthetic calculation with its timestamps fixed. openpyxl stamps the time it writes into the workbook's
    properties and every zip entry, and a calc post's id starts with the file's sha256, so without this the
    calculation block would move from one run of the suite to the next."""
    out = io.BytesIO()
    with zipfile.ZipFile(xlsx) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "docProps/core.xml":
                data = re.sub(rb"(<dcterms:(created|modified)[^>]*>)[^<]*", rb"\g<1>2026-01-01T00:00:00Z", data)
            dst.writestr(zipfile.ZipInfo(info.filename, date_time=(2026, 1, 1, 0, 0, 0)), data, zipfile.ZIP_DEFLATED)
    xlsx.write_bytes(out.getvalue())


def _first_requests(folder: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, str], dict[str, str]]:
    """The map's first request, by part, and each lens's first message, from one run of the synthetic tender."""
    _fixed_bytes(folder / "calc" / "kalkyl.xlsx")
    sdk = ScriptedSDK(script)
    monkeypatch.setattr(model_client, "keychain_key", lambda **_: "sk-test")
    monkeypatch.setattr(model_client, "make_sdk_client", lambda config, key: sdk)
    assert cli.main(["run", "co-001", "t-0001", "--run-id", "t-0001-pins"]) == 0
    first = [p for p in sdk.sent if p["system"] != CARDS_SYSTEM and len(p["messages"]) == 1]
    map_request = next(p for p in first if "Map this tender" in first_text(p))
    other = {k: v for k, v in map_request.items() if k not in ("system", "tools", "messages")}
    parts = {
        "system": _sha(map_request["system"]),
        "tools": _sha(map_request["tools"]),
        "map_message": _sha(map_request["messages"]),
        "params": _sha(other),
    }
    lenses: dict[str, str] = {}
    for p in first:
        text = first_text(p)
        if "review lens `" in text:
            lenses.setdefault(text.split("review lens `")[1].split("`")[0], _sha(p["messages"]))
    return parts, lenses


def _moved(pins: dict[str, str], now: dict[str, str]) -> list[str]:
    return sorted(k for k in pins.keys() | now.keys() if pins.get(k) != now.get(k))


def test_the_map_request_for_a_fixed_input_is_pinned(
    tender: Path,  # noqa: F811 - the fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: `Code reads only these names` -> `Code reads only those names` in `prefix.formula_text`: the brief,
    one of the system blocks every Opus role sends, moves by one word."""
    parts, _ = _first_requests(tender, monkeypatch)
    assert not _moved(MAP_PINS, parts), f"{_moved(MAP_PINS, parts)}: {MOVED}"


def test_each_lens_first_message_for_a_fixed_input_is_pinned(
    tender: Path,  # noqa: F811 - the fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: `call submit_flags once` -> `call submit_flags one time` in `run.lens_message`: every lens's first
    message moves."""
    _, lenses = _first_requests(tender, monkeypatch)
    assert not _moved(LENS_PINS, lenses), f"{_moved(LENS_PINS, lenses)}: {MOVED}"
