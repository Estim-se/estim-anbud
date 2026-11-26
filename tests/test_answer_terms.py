from __future__ import annotations

import pytest

from anbud.answer_terms import classify, classify_path, fold


def test_fold() -> None:
    assert fold("Frågor & Svar_2") == "fragor svar 2"
    assert fold("ÄNDRING-01.pdf") == "andring 01 pdf"


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("Frågor och svar 1", "qa"),
        ("frågor_svar", "qa"),
        ("F&S nr 3", "qa"),
        ("Q&A", "qa"),
        ("Svar på frågor", "qa"),
        ("Fragor2", "qa"),
        ("Ändring 1 av förfrågningsunderlaget", "amendment"),
        ("Rättelse", "amendment"),
        ("KFU 02", "amendment"),
        ("Kompletterande förfrågningsunderlag", "amendment"),
        ("Förtydligande", "amendment"),
        ("PM 1", "amendment"),
        ("PM nr 2", "amendment"),
        ("PM02", "amendment"),
        ("Tilldelningsbeslut", "award"),
        ("Öppningsprotokoll", "award"),
        ("Beslut om tilldelning", "award"),
        ("Protokoll från anbudsöppning", "award"),
        ("Frågor och svar samt ändring", "amendment"),
    ],
)
def test_answer_names(name: str, kind: str) -> None:
    assert classify(name) == kind


@pytest.mark.parametrize(
    "name",
    [
        "Anbudsförfrågan",  # contains the word for "question" but is the request for tenders
        "Förfrågningsunderlag",
        "Administrativa föreskrifter",
        "Teknisk beskrivning el",
        "PM Geoteknik",
        "Svarsformulär",
        "Anbudsformulär",
        "Mängdförteckning",
        "Ritningsförteckning",
        "Utvärderingsmodell",
        "Tilläggsisolering",
        "Kontraktsförslag",
    ],
)
def test_tender_documents_are_not_held(name: str) -> None:
    assert classify(name) is None


def test_paths() -> None:
    assert classify_path(("Underlag", "Frågor och svar", "nr 1.pdf")) == "qa"
    assert classify_path(("Underlag", "Teknisk beskrivning.pdf")) is None
    assert classify_path(("Underlag", "svar.pdf")) == "qa"
    assert classify_path(("fos",)) == "qa"
    assert classify_path(()) is None


# Review findings on PR #8: compound and variant names that slipped through, loose words that held ordinary tender
# documents, and folders named after the tender that held a whole delivery.


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("Kompletteringsdokument 1", "amendment"),
        ("Kompletteringsunderlag", "amendment"),
        ("Kompletterings-PM", "amendment"),
        ("Ändrings-PM", "amendment"),
        ("Ändringsförteckning", "amendment"),
        ("Anbudsfrågor", "qa"),
        ("Frågelista", "qa"),
        ("FrågorOchSvar", "qa"),
        ("FAQ", "qa"),
        ("F o S 1", "qa"),
        ("FS_1", "qa"),
        ("FoS 3", "qa"),
        ("Upphandlingsbeslut", "award"),
        ("Change notice", "amendment"),
        ("Notice of modification", "amendment"),
        ("Meddelande till anbudsgivare nr 1", "amendment"),
        ("Förfrågningar under anbudstiden", "qa"),
        ("PM nummer 2", "amendment"),
        ("Svar 2", "qa"),
        ("Tillägg 1 till förfrågningsunderlaget", "amendment"),
        ("Tilldelning", "award"),
        ("Ändring av sista anbudsdag", "amendment"),
        ("Contract award notice", "award"),
    ],
)
def test_compound_and_variant_answer_names_are_held(name: str, kind: str) -> None:
    assert classify(name) == kind


@pytest.mark.parametrize(
    "name",
    [
        "OVK-protokoll",
        "Protokoll provtagning asbest",
        "Kriterier för tilldelning",
        "Utvärdering och tilldelning",
        "Tilldelningskriterier",
        "Kompletterande föreskrifter",
        "Tillägg prislista",
        "PM 01 Geoteknik",
        "Bilaga 3 svar på krav",
        "Award criteria",
    ],
)
def test_loose_words_with_ordinary_words_hold_nothing(name: str) -> None:
    assert classify(name) is None


@pytest.mark.parametrize(
    "title", ["Kompletterande markarbeten etapp 2", "Tillägg förskola", "Ändring av ventilation skola"]
)
def test_a_folder_or_zip_named_after_the_tender_holds_nothing(title: str) -> None:
    assert classify(title) is None
    assert classify_path((title, "AF.pdf")) is None
    assert classify_path((title, "Ritningar", "A-101.pdf")) is None
    assert classify_path((title, "Frågor och svar", "nr 1.pdf")) == "qa"
