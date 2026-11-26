"""validate_flags on a synthetic corpus."""

from __future__ import annotations

from _granskning_synth import corpus, flag, search

from anbud.granskning.validate import claim_numbers, quote_matches, repair_message, validate_flags


def _check(*flags, log=None):
    return validate_flags([f.model_dump(mode="json") for f in flags], log or [], corpus())


def test_a_grounded_flag_passes() -> None:
    valid, rejects = _check(flag())
    assert len(valid) == 1 and rejects == []


def test_an_unknown_block_and_a_made_up_quote_are_rejected() -> None:
    _, rejects = _check(
        flag(cites=[("nope-000000", "Byggström tillhandahålls")]),
        flag(cites=[("aaa111-000004", "Byggström ingår i entreprenaden")]),
    )
    assert "does not exist" in rejects[0]["reasons"][0]
    assert "quote is not in block aaa111-000004" in rejects[1]["reasons"][0]
    assert [r["index"] for r in rejects] == [0, 1]


def test_quotes_tolerate_normalisation_hyphenation_and_skips() -> None:
    text = "Entreprenören ska utföra all elin- stallation i trapphusen, totalt 24 armaturer."
    assert quote_matches("all elinstallation i trapphusen", text)  # the word whole
    assert quote_matches("all elin- stallation i trapphusen", text)  # as prep joined it
    assert quote_matches("ENTREPRENÖREN   ska utföra", text)  # case and whitespace
    assert quote_matches("Entreprenören ska … totalt 24 armaturer", text)  # an ellipsis skip, in order
    assert not quote_matches("totalt 24 armaturer … Entreprenören", text)  # out of order
    assert not quote_matches("ska", text)  # too short to mean anything
    assert quote_matches("Vite utgår med 5 000 kr – per", "Vite utgår med 5 000 kr – per vecka")


def test_numbers_in_the_claim_must_be_quoted() -> None:
    cites = [("aaa111-000003", "Vite utgår med 5 000 kr per påbörjad vecka")]
    ok = flag(claim="Vitet är 5000 kr per vecka enligt ABT 06 kap 5 § 3 och AFC.32.", cites=cites)
    bad = flag(claim="Vitet är 7 500 kr per vecka i 12 veckor.", cites=cites)
    valid, rejects = _check(ok, bad)
    assert len(valid) == 1
    assert rejects[0]["reasons"] == ["numbers in the claim that no quote contains: 7 500, 12"]


def test_reference_numbers_are_exempt_but_quantities_are_not() -> None:
    assert claim_numbers("Enligt BFS 2026:4 och SS 436 40 00 ska 12,5 m kabel dras, se kap 3.") == ["12,5"]
    assert claim_numbers("en 5 meters sträcka") == ["5"]


def test_an_absence_needs_searches_from_the_log_and_a_rule() -> None:
    absent = dict(absence=True, searches=["provisorisk belysning"], reference_rule="Normal omfattning vid stambyte")
    log = [search("Provisorisk  belysning"), search("byggström", refused="not available to this role")]
    valid, rejects = _check(flag(**absent), log=log)
    assert len(valid) == 1 and not rejects

    _, rejects = _check(
        flag(absence=True, reference_rule="x"),
        flag(**{**absent, "searches": ["byggström"]}),
        flag(**{**absent, "reference_rule": None}),
        log=log,
    )
    assert "must list the searches" in rejects[0]["reasons"][0]
    assert "never ran: 'byggström'" in rejects[1]["reasons"][0]  # a refused search doesn't count
    assert "reference rule" in rejects[2]["reasons"][0]


def test_a_malformed_flag_is_a_reject_not_a_crash() -> None:
    valid, rejects = validate_flags([{"lens_id": "x", "claim": "y"}], [], corpus())
    assert valid == [] and rejects[0]["index"] == 0
    assert any(r.startswith("citations") for r in rejects[0]["reasons"])
    message = repair_message(rejects)
    assert "flag 1:" in message and "submit_flags again" in message
