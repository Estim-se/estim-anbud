"""Merge: code clusters, and the optional Sonnet merge is accepted only when it keeps every citation. $0."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from _granskning_synth import basis, corpus, flag, lens_result
from anthropic.types import Usage

from anbud.config import load_config
from anbud.granskning.contracts import fingerprint
from anbud.granskning.merge import MERGE_SYSTEM, clusters, items_from_results, merge
from anbud.model_client import Budget, ModelClient

TRAPPHUS = ("aaa111-000002", "all elinstallation i trapphusen")
ARMATURER = ("aaa111-000002", "totalt 24 armaturer")
BYGGSTROM = ("aaa111-000004", "Byggström tillhandahålls av beställaren")
UTTAG = ("bbb222-000001", "Rum 101 | Uttag | 12 st")


def _tool_use(flags: list[dict[str, Any]]) -> SimpleNamespace:
    block = SimpleNamespace(type="tool_use", name="submit_flags", input={"flags": flags}, id="t1")
    return SimpleNamespace(content=[block], stop_reason="tool_use")


class FakeClient:
    def __init__(self, flags: list[dict[str, Any]]) -> None:
        self.flags = flags
        self.calls: list[dict[str, Any]] = []

    def call(self, **kw: Any) -> SimpleNamespace:
        self.calls.append(kw)
        return SimpleNamespace(message=_tool_use(self.flags))


def _two_lenses():
    a = flag("trade_boundaries", "Trapphusbelysningen ligger på el.", [TRAPPHUS], claim_type="trade_boundary")
    b = flag("quantities", "Kalkylen saknar 24 armaturer.", [ARMATURER], claim_type="quantity")
    c = flag("contract", "Byggström saknas.", [BYGGSTROM])
    return [lens_result("trade_boundaries", [a, c]), lens_result("quantities", [b])], (a, b, c)


def test_same_fingerprint_flags_become_one_item_with_every_lens() -> None:
    a = flag("trade_boundaries", cites=[BYGGSTROM])
    b = flag("generic_scope", cites=[BYGGSTROM, BYGGSTROM], searches=["byggström"])
    items = items_from_results([lens_result("trade_boundaries", [a]), lens_result("generic_scope", [b], generic=True)])
    (item,) = items
    assert item.lenses == ["trade_boundaries", "generic_scope"]
    assert item.generic is False  # a specific lens found it too
    assert item.fingerprint == fingerprint(a) and len(item.flag.citations) == 1


def test_clusters_join_shared_blocks_and_calc_posts() -> None:
    results, _ = _two_lenses()
    post_a = flag("x", "Uttag saknas.", [UTTAG], money_basis=basis(kind="quantity_gap", post_ref="calc-1", quantity=2))
    post_b = flag("y", "Byggström.", [BYGGSTROM], claim_type="other", money_basis=basis(post_ref="calc-1"))
    items = items_from_results([*results, lens_result("z", [post_a, post_b])])
    sizes = sorted(len(c) for c in clusters(items))
    assert sizes == [2, 3]  # trapphus+armaturer share a block; byggström joins uttag through calc-1


def test_without_a_client_nothing_different_is_merged() -> None:
    results, _ = _two_lenses()
    assert len(merge(results, corpus())) == 3


def test_a_model_merge_that_keeps_every_citation_is_accepted() -> None:
    results, (a, b, _) = _two_lenses()
    merged = flag("trade_boundaries", "Trapphusbelysningen med 24 armaturer saknas i kalkylen.", [TRAPPHUS, ARMATURER])
    client = FakeClient([merged.model_dump(mode="json")])
    items = merge(results, corpus(), client)
    assert len(items) == 2
    joined = next(i for i in items if len(i.flag.citations) == 2)
    assert joined.lenses == ["trade_boundaries", "quantities"]
    assert set(joined.sources) == {fingerprint(a), fingerprint(b)}
    (call,) = client.calls
    assert call["role"] == "merge" and call["system"][0]["text"] == MERGE_SYSTEM
    assert "<clusters>" in call["messages"][0]["content"]
    assert "Byggström saknas" not in call["messages"][0]["content"]  # single-flag clusters aren't sent


def test_a_model_merge_that_drops_adds_or_invents_falls_back() -> None:
    results, _ = _two_lenses()
    dropped = flag("t", "Trapphusbelysningen saknas.", [TRAPPHUS])
    added = flag("t", "Trapphusbelysningen saknas.", [TRAPPHUS, ARMATURER, BYGGSTROM])
    invented = flag("t", "Trapphusbelysningen med 30 armaturer saknas.", [TRAPPHUS, ARMATURER])
    for bad in (dropped, added, invented):
        items = merge(results, corpus(), FakeClient([bad.model_dump(mode="json")]))
        assert len(items) == 3
    assert len(merge(results, corpus(), FakeClient([{"claim": "not a flag"}]))) == 3


class _SDK:
    def __init__(self, content: list[Any]) -> None:
        self.content = content
        self.sent: list[dict[str, Any]] = []
        self.messages = self

    def count_tokens(self, **_: Any) -> SimpleNamespace:
        return SimpleNamespace(input_tokens=500)

    def stream(self, **params: Any) -> Any:
        self.sent.append(params)
        usage = Usage(input_tokens=500, output_tokens=50, cache_creation_input_tokens=0, cache_read_input_tokens=0)
        message = SimpleNamespace(usage=usage, stop_reason="tool_use", content=self.content)
        return _Ctx(SimpleNamespace(request_id="req", get_final_message=lambda: message))


class _Ctx:
    def __init__(self, stream: Any) -> None:
        self.stream = stream

    def __enter__(self) -> Any:
        return self.stream

    def __exit__(self, *_: object) -> None:
        return None


def test_the_merge_goes_through_model_client_on_sonnet() -> None:
    results, _ = _two_lenses()
    merged = flag("trade_boundaries", "Trapphusbelysningen med 24 armaturer saknas.", [TRAPPHUS, ARMATURER])
    sdk = _SDK(_tool_use([merged.model_dump(mode="json")]).content)
    client = ModelClient(load_config(), sdk, Budget(1.0), run_id="r-1", company_id="co-001")
    assert len(merge(results, corpus(), client)) == 2
    (params,) = sdk.sent
    assert params["model"] == "claude-sonnet-5-5" and params["max_tokens"] == 8000
    assert params["tool_choice"] == {"type": "auto"} and len(params["tools"]) == 10
