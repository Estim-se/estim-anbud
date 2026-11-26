"""Synthetic text written for these tests; none of it comes from a licensed source."""

from __future__ import annotations

from anbud.guards import licensed_text

LONG = " ".join(["detta är en påhittad mening för testet"] * 10)  # 70 words, invented
SHORT = "kontrollera att kabeln är märkt"


def test_long_quote_in_prose_is_flagged() -> None:
    text = f'Inledning.\n\nKravet lyder "{LONG}" och sedan slut.\n'
    assert licensed_text.check("lenses/contract.md", text) == [(3, "quoted passage of 30+ words")]


def test_typographic_quotes_and_blockquotes_are_flagged() -> None:
    assert licensed_text.check("docs/a.md", f"Texten: “{LONG}”\n")
    blockquote = "\n".join(f"> {LONG[i : i + 40]}" for i in range(0, len(LONG), 40))
    assert licensed_text.check("docs/b.md", blockquote + "\n")


def test_short_quotes_pass() -> None:
    assert licensed_text.check("lenses/contract.md", f'Skriv "{SHORT}" i checklistan.\n') == []


def test_long_paragraph_next_to_a_standard_reference_is_flagged_in_any_text_file() -> None:
    text = f"# Enligt ABT 06 kap 4\n{LONG}\n"
    assert licensed_text.check("src/anbud/lenses_data.py", text)
    assert licensed_text.check("lenses/x.md", f"AMA {LONG}\n")


def test_long_paragraph_without_a_reference_passes() -> None:
    assert licensed_text.check("docs/plan.md", LONG + "\n") == []


def test_a_short_paraphrase_citing_a_clause_passes() -> None:
    assert licensed_text.check("lenses/contract.md", "- ABT 06 kap 5 §3: vite per vecka, kontrollera tak.\n") == []


def test_long_docstrings_in_code_are_not_treated_as_quotes() -> None:
    assert licensed_text.check("src/anbud/x.py", f'"""{LONG}"""\n') == []


def test_tests_and_fixtures_are_out_of_scope() -> None:
    assert licensed_text.check("tests/test_x.py", f'AMA "{LONG}"\n') == []
    assert licensed_text.check("fixtures/synthetic/a.md", f'AMA "{LONG}"\n') == []


def test_currency_code_alone_is_not_a_marker() -> None:
    assert licensed_text.check("docs/cost.md", f"Priset i SEK. {LONG}\n") == []


def test_newer_contract_editions_are_markers_too() -> None:
    for reference in ("ABT 25", "AB 25", "ABT-U 07", "AB 09"):
        assert licensed_text.check("docs/x.md", f"{reference}: {LONG}\n"), reference


def test_markers_with_a_no_break_space_or_hyphen_count() -> None:
    for reference in ("ABT 06", "AB 04", "ABT-06"):
        assert licensed_text.check("docs/x.md", f"{reference}: {LONG}\n"), reference
