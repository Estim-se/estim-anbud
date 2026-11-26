"""The verifier's pieces: selection, the first message, the verdict check and the plug for the loop."""

from __future__ import annotations

from typing import Any

from _granskning_synth import corpus, flag, lens_result

from anbud.granskning.items import Item
from anbud.granskning.merge import items_from_results
from anbud.granskning.verify import run_verifier, select, validate_verdict, verifier_message
from anbud.models import Verdict

SYSTEM = [{"type": "text", "text": "Synthetic shared system block."}]


def _items(n: int) -> list[Item]:
    flags = [flag(f"lens{i}", f"Påstående {i}.", claim_type=f"type{i}") for i in range(n)]
    return items_from_results([lens_result("l", flags)])


def _verdict(fp: str, **kw: Any) -> dict[str, Any]:
    return {
        "fingerprint": fp,
        "verdict": "confirmed",
        "electrical_scope": True,
        "covered_by_calc": False,
        "would_be_wrong_if": "Beställaren tar byggströmmen i ett annat dokument.",
        **kw,
    }


def test_selection_takes_the_top_and_a_seeded_sample_below_the_cut() -> None:
    items = _items(40)
    value = {i.fingerprint: n for n, i in enumerate(items)}
    chosen = select(items, lambda i: value[i.fingerprint], 25, 3, "run-1")
    top = [i for i, how in chosen if how == "verified"]
    sample = [i for i, how in chosen if how == "sample"]
    assert [value[i.fingerprint] for i in top] == list(range(39, 14, -1))
    assert len(sample) == 3 and all(value[i.fingerprint] < 15 for i in sample)
    again = select(list(reversed(items)), lambda i: value[i.fingerprint], 25, 3, "run-1")
    assert [i.fingerprint for i, _ in again] == [i.fingerprint for i, _ in chosen]
    assert len(select(items[:5], lambda i: 1.0, 25, 3, "s")) == 5


def test_the_message_has_the_claim_and_context_but_not_the_lens_reasoning() -> None:
    f = flag(
        cites=[("aaa111-000003", "Vite utgår med 5 000 kr")],
        absence=True,
        searches=["sökord som linsen körde"],
        reference_rule="Avtalets vitesbestämmelse",
    )
    (item,) = items_from_results([lens_result("contract", [f])])
    message = verifier_message(item, corpus())
    assert item.fingerprint in message and f.claim in message and "refute" in message
    assert "Byggström tillhandahålls av beställaren." in message  # the neighbouring block
    assert "3.2 Elinstallationer" not in message  # two blocks away
    assert "<tender_corpus>" in message and "untrusted" in message
    assert "sökord som linsen körde" not in message
    assert "kronor" not in message.lower() and "money_basis" in message


def test_verdicts_are_checked() -> None:
    fp = "f" * 64
    verdict, reasons = validate_verdict(_verdict(fp), fp)
    assert isinstance(verdict, Verdict) and reasons == []
    _, reasons = validate_verdict(_verdict("x"), fp)
    assert reasons == [f'fingerprint must be "{fp}"']
    _, reasons = validate_verdict(_verdict(fp, covered_by_calc=True), fp)
    assert "needs the calc_post" in reasons[0]
    _, reasons = validate_verdict(_verdict(fp, covered_by_calc=True, calc_post="calc-9"), fp, {"calc-1"})
    assert reasons == ["calc post calc-9 does not exist"]
    _, reasons = validate_verdict({"verdict": "maybe"}, fp)
    assert reasons and all(":" in r for r in reasons)


def test_run_verifier_plugs_into_the_loop_with_the_shared_system_blocks() -> None:
    items = _items(3)
    seen: list[dict[str, Any]] = []

    def loop(*, step: str, system_blocks: list[dict], user_message: str, validate: Any) -> Verdict | None:
        seen.append({"step": step, "system": system_blocks, "message": user_message})
        fp = next(i.fingerprint for i in items if i.fingerprint in user_message)
        if fp == items[2].fingerprint:
            return None
        bad, reasons = validate(_verdict("wrong"))
        assert bad is None and reasons
        good, _ = validate(_verdict(fp, verdict="partly"))
        return good

    chosen = [(items[0], "verified"), (items[1], "sample"), (items[2], "verified")]
    out = run_verifier(chosen, corpus(), SYSTEM, loop, parallel=2)
    assert [i.verification for i in out] == ["verified", "sample", "failed"]
    assert out[0].verdict.verdict == "partly" and out[2].verdict is None
    assert all(s["system"] is SYSTEM for s in seen)
    assert {s["step"] for s in seen} == {f"verify:{i.fingerprint[:16]}" for i in items}
