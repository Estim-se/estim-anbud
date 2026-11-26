from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FAKE_NAMES, assert_no_fake_names

from anbud.guards import names
from anbud.guards.__main__ import main as guards_main


def matcher() -> names.Matcher:
    return names.Matcher.from_entries(names.parse_plain(FAKE_NAMES))


@pytest.mark.parametrize(
    "text",
    [
        "Offert till Fiktivbolaget AB i dag",
        "offert till FIKTIVBOLAGET ab",  # case-insensitive
        "fiktivbolaget-AB",  # punctuation between words
        "Påhittade   Fastigheter",  # extra spaces
        "pahittade fastigheter",  # accents optional
        "kontakt: Orjan Exempelsson",
        "ring 555-123 45 67",
        "ring 555 123 45 67",
        "Ampere sa nej",  # case-sensitive entry, exact case
    ],
)
def test_finds_listed_names(text: str) -> None:
    assert matcher().find_lines(text) == [1]


@pytest.mark.parametrize(
    "text",
    [
        "Fiktivbolagetx AB",  # not a whole word
        "Fiktivbolaget utan suffix",  # the entry is two words
        "230 ampere säkring",  # case-sensitive entry, other case
        "Påhittade utan fastigheter",
        "ring 555-123",
    ],
)
def test_ignores_non_matches(text: str) -> None:
    assert matcher().find_lines(text) == []


def test_reports_the_line_where_a_name_starts_even_across_a_line_break() -> None:
    text = "rad ett\nrad två med Påhittade\nFastigheter här\n"
    assert matcher().find_lines(text) == [2]


def test_hashed_list_finds_the_same_names_and_holds_no_readable_name() -> None:
    entries = names.parse_plain(FAKE_NAMES)
    hashed = names.render_hashed(entries)
    assert_no_fake_names(hashed)
    m = names.Matcher.from_hashed(hashed)
    assert m.find_lines("svar från fiktivbolaget ab") == [1]
    assert m.find_lines("Ampere") == [1]
    assert m.find_lines("ampere") == []


def test_fails_closed_without_a_list(tmp_path: Path) -> None:
    with pytest.raises(names.NamesMissing):
        names.load_matcher({}, tmp_path / "missing.txt")
    with pytest.raises(names.NamesMissing):
        names.load_matcher({"CI": "true"}, tmp_path / "missing.txt")


def test_ci_never_falls_back_to_a_local_file(names_file: Path) -> None:
    with pytest.raises(names.NamesMissing):
        names.load_matcher({"CI": "true"}, names_file)


def test_empty_or_malformed_lists_fail_closed(tmp_path: Path) -> None:
    empty = tmp_path / "empty.txt"
    empty.write_text("# only a comment\n", encoding="utf-8")
    with pytest.raises(names.NamesMissing):
        names.load_matcher({}, empty)
    with pytest.raises(names.NamesMissing):
        names.load_matcher({"ANBUD_NAMES_HMAC": "not a list"}, empty)


def test_hash_names_command_prints_hashes_only(names_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert guards_main(["hash-names", "--names", str(names_file)], env={}) == 0
    out, err = capsys.readouterr()
    assert out.startswith(names.HEADER)
    assert_no_fake_names(out, err)


@pytest.mark.parametrize(
    "text",
    [
        "Påhittade Fastigheters offert",  # Swedish genitive
        "Amperes svar",  # genitive of a case-sensitive entry
        "se FiktivbolagetAb för detaljer",  # camelCase join
        "www.fiktivbolagetab.se",  # a multi-word entry written as one word
        "Fiktiv­bolaget AB",  # soft hyphen
        "Fiktivbolaget​ AB",  # zero-width space
        "ring 5551234567",  # a number written without spaces
    ],
)
def test_finds_joined_and_inflected_forms(text: str) -> None:
    assert matcher().find_lines(text) == [1]


@pytest.mark.parametrize("line", ["Fiktivbolaget AB # kund 2026", "Fiktivbolaget AB (kund)", "[x] Fiktivbolaget AB"])
def test_annotations_on_an_entry_line_are_refused_without_echoing_it(line: str) -> None:
    with pytest.raises(names.NamesMissing) as info:
        names.parse_plain(f"# ok\n{line}\n")
    assert "line 2" in str(info.value)
    assert_no_fake_names(str(info.value))


def test_overlong_entries_are_refused() -> None:
    with pytest.raises(names.NamesMissing):
        names.parse_plain("ett två tre fyra fem sex sju åtta nio\n")


def test_joins_that_only_the_splitter_finds() -> None:
    m = matcher()
    assert m.find_lines("rapportFiktivbolagetAB") == [1]  # neither the plain token nor the squashed form matches
    assert m.find_lines("SvarFrånÖrjanExempelsson") == [1]


@pytest.mark.parametrize(
    "text",
    [
        "Fiktiv​bolaget AB",  # zero-width space inside a word
        "offert​Fiktivbolaget​AB​kund",  # zero-width spaces as the only word breaks
        "Fiktiv͏bolaget AB",  # combining grapheme joiner
        "Fiktiv️bolaget AB",  # variation selector
    ],
)
def test_invisible_characters_neither_hide_nor_glue_names(text: str) -> None:
    assert matcher().find_lines(text) == [1]


def test_short_entries_do_not_match_hashes_or_double_s_words() -> None:
    m = names.Matcher.from_entries(names.parse_plain("Bea\nClas\n"))
    assert m.find_lines('sha256 = "e96195ff961bea2ab0f4c1d8e3a7b9f0"') == []
    assert m.find_lines("class Foo: pass") == []
    assert m.find_lines("Bea ringde") == [1] and m.find_lines("Clas ringde") == [1]


def test_a_list_saved_with_a_bom_or_fullwidth_marks_parses_strictly() -> None:
    assert len(names.parse_plain("﻿# lista\nFiktivbolaget AB\n")) == 1
    for line in ("Fiktivbolaget AB （kund）", "Fiktivbolaget AB, Påhittade Fastigheter", "Fiktivbolaget AB / kund"):
        with pytest.raises(names.NamesMissing):
            names.parse_plain(line + "\n")


def test_an_unreadable_list_or_a_damaged_secret_fails_closed(tmp_path: Path) -> None:
    latin = tmp_path / "latin.txt"
    latin.write_bytes("Påhittade Fastigheter\n".encode("cp1252"))
    with pytest.raises(names.NamesMissing):
        names.load_matcher({}, latin)
    with pytest.raises(names.NamesMissing):
        names.load_matcher({}, tmp_path)  # a directory
    good = names.render_hashed(names.parse_plain(FAKE_NAMES))
    for bad in (good.replace(" 2 ", " 99 ", 1), good.rstrip("\n") + "\ni 1 abc\n"):
        with pytest.raises(names.NamesMissing):
            names.Matcher.from_hashed(bad)
