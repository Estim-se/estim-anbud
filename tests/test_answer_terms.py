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
