from __future__ import annotations

import pytest
from synthetic_docs import personnummer

from anbud.prep.masking import Cell, Masker, markings


def masked(text: str) -> tuple[str, list[str]]:
    out, spans = Masker().text(text)
    return out, [s.kind for s in spans]


def test_security_markings_stop_but_ordinary_words_do_not() -> None:
    assert markings("Handling: HEMLIG enligt beslut") == ["hemlig"]
    assert markings("Krav på säkerhetsskyddsavtal kan ställas") == ["sakerhetsskydd"]
    assert markings("en hemlig ingång i källaren") == []
    assert markings("Allmänna bestämmelser") == []


def test_checksum_valid_personnummer_is_masked_and_invalid_is_not() -> None:
    valid = personnummer("811218")
    invalid = valid[:-1] + str((int(valid[-1]) + 1) % 10)
    assert masked(f"Kontakt {valid} dagtid") == ("Kontakt [personnummer] dagtid", ["personnummer"])
    assert masked(f"Kontakt {invalid} dagtid")[1] == []
    assert masked(f"Kontakt 19{valid} dagtid")[1] == ["personnummer"]


def test_organisation_numbers_and_samordningsnummer() -> None:
    org = "556" + "012" + "-" + "3456"  # month field 20+: an organisation number shape, never masked
    assert masked(f"Org.nr {org}")[1] == []
    samordning = personnummer("811278")  # day 78 = 18 + 60
    assert masked(f"Nr {samordning}")[1] == ["personnummer"]


def test_email_and_iban() -> None:
    assert masked("Skicka till anna.test@example.se senast fredag") == (
        "Skicka till [e-post] senast fredag",
        ["email"],
    )
    assert masked("Konto SE45 5000 0000 0583 9825 7466 gäller")[1] == ["iban"]  # the published example IBAN
    assert masked("Konto SE45 5000 0000 0583 9825 7467 gäller")[1] == []


@pytest.mark.parametrize(
    "text",
    ["Tel: 08-111 222 33", "TEL 070 – 111 22 33", "telefon 0701112233", "Mobil +46 70 111 22 33"],
)
def test_phone_numbers_after_a_label_are_masked(text: str) -> None:
    out, kinds = masked(text)
    assert kinds == ["phone"]
    assert out.endswith("[telefon]")


def test_phone_after_an_email_is_masked_and_an_unlabelled_number_is_not() -> None:
    assert masked("E-post a.b@example.se 070-111 22 33")[1] == ["email", "phone"]
    assert masked("Lägenhet 070-111-2233 i trapphus B")[1] == []


def test_short_numbers_survive_for_the_number_checks() -> None:
    assert masked("Tel: 12 345 67")[1] == []  # seven digits: an E-number shape


def test_two_numbers_after_one_label() -> None:
    out, kinds = masked("Tel: 08-111 222 33, 070-111 22 33")
    assert kinds == ["phone", "phone"]
    assert out == "Tel: [telefon], [telefon]"


def test_span_offsets_point_at_the_tokens() -> None:
    text = f"A {personnummer('811218')} och b@example.se"
    out, spans = Masker().text(text)
    for span in spans:
        assert out[span.start : span.end].startswith("[")
        assert span.original in text


def test_sheet_cells_numeric_cells_are_never_masked_and_names_become_tokens() -> None:
    m = Masker()
    pnr = personnummer("811218")
    out, spans = m.cells(
        [
            Cell("A", pnr.replace("-", ""), numeric=True),
            Cell("B", "Kim Exempel", name_column=True),
            Cell("C", pnr),
        ]
    )
    assert out == f"{pnr.replace('-', '')} | [namn 1] | [personnummer]"
    assert [s.kind for s in spans] == ["name", "personnummer"]
    for s in spans:
        assert out[s.start : s.end].startswith("[")
    again, _ = m.cells([Cell("B", "kim exempel", name_column=True), Cell("B", "Ny Person", name_column=True)])
    assert again == "[namn 1] | [namn 2]"
