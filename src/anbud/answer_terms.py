"""Words that mark a file, a folder or a section as part of an answer key.

The answer keys for a public tender are the buyer's own questions and answers, its amendments, and the award decision
and opening protocol. They must never reach a run as input: blind intake moves matching files of a dev tender to
`answers/<co>/<t>/intake/` before prep, and holds a notice page that carries such sections, putting a copy without
them in its place. One rule serves every caller, so they can't disagree.

Matching is on whole words after folding (case, accents, punctuation, CamelCase), so a word that only contains a term
doesn't match: the Swedish word for a request for tenders contains the word for "question" and must not be caught.
A few compound heads are matched as prefixes ("Kompletteringsunderlag", "Ändrings-PM").

Strong words hold a name wherever they appear. Weak words ("svar", "tillägg", "tilldelning", "protokoll" ...) also
appear in ordinary tender documents and in tender titles ("Kriterier för tilldelning", "OVK-protokoll", "Tillägg
förskola"), so they hold a name only when every other word in it is an answer word, a filler word or a number:
"Svar 2", "Tillägg till förfrågningsunderlaget", "Tilldelning".

When in doubt a term is listed: a document wrongly held back costs a little input, an answer that slips through
spoils the blind score. Every hold is counted, and the local manifest lists what was held, so a dev tender's holds
can be checked once its first run is frozen.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

AnswerKind = Literal["qa", "amendment", "award"]

# Single words, after folding, that hold a name wherever they appear. Compound words are listed whole.
_STRONG: dict[str, AnswerKind] = {
    # Questions and answers
    **dict.fromkeys(
        (
            "fraga", "fragan", "fragor", "fragorna", "fragesvar", "fragorsvar", "fragestallning", "fragestallningar",
            "fragelista", "fragelistan", "fos", "qa", "faq", "questions", "question", "answers", "clarification",
            "clarifications",
        ),
        "qa",
    ),
    # Amendments and corrections
    **dict.fromkeys(
        (
            "andringen", "andringarna", "andringsmeddelande", "andringsannons", "andringsmeddelanden",
            "rattelse", "rattelser", "rattelsen", "rattelsemeddelande", "komplettering", "kompletteringar",
            "kompletteringen", "kompletteringsbrev", "kompletteringspm", "kfu", "fortydligande", "fortydliganden",
            "fortydligandet", "tillaggsinformation", "tillaggsbrev", "corrigendum", "addendum", "addenda",
            "amendment", "amendments", "erratum", "errata",
        ),
        "amendment",
    ),
    # Award decisions, opening protocols and evaluation results
    **dict.fromkeys(
        (
            "tilldelningsbeslut", "tilldelningsbeslutet", "tilldelningsmeddelande", "oppningsprotokoll",
            "anbudsoppning", "anbudsoppningsprotokoll", "anbudsoppningen", "utvarderingsrapport",
            "utvarderingsprotokoll", "upphandlingsrapport", "upphandlingsprotokoll", "upphandlingsbeslut",
            "avtalsbeslut", "kontraktstilldelning", "antagandebesked", "anbudssammanstallning", "awarded",
            "tabulation",
        ),
        "award",
    ),
}  # fmt: skip

# Weak words: they hold a name only when the whole name is answer words, filler words and numbers.
_WEAK: dict[str, AnswerKind] = {
    **dict.fromkeys(("svar", "svaren", "svaret", "fs"), "qa"),
    **dict.fromkeys(("andring", "andringar", "tillagg", "kompletterande"), "amendment"),
    **dict.fromkeys(("tilldelning", "tilldelningen", "protokoll", "protokollet", "award"), "award"),
}

# Compound heads, matched as the start of a word, with the words that start the same way and aren't answer keys.
_PREFIXES: tuple[tuple[str, AnswerKind, tuple[str, ...]], ...] = (
    ("fragor", "qa", ()),  # "fragorochsvar" written as one word
    ("andrings", "amendment", ("andringsarbete",)),
    ("kompletterings", "amendment", ()),
    ("rattelse", "amendment", ()),
    ("fortydligand", "amendment", ()),
    ("tilldelningsbeslut", "award", ()),
    ("tilldelningsmeddelande", "award", ()),
    ("oppningsprotokoll", "award", ()),
    ("anbudsoppning", "award", ()),
)
# Compound tails: "Anbudsfrågor", "Inkomna frågor" written as one word.
_SUFFIXES: tuple[tuple[str, AnswerKind], ...] = (("fragor", "qa"), ("fragorna", "qa"))

# Short phrases, as folded word sequences: initials and terms that are too loose word by word. They hold a name
# wherever they appear.
_PHRASES: tuple[tuple[tuple[str, ...], AnswerKind], ...] = (
    (("f", "s"), "qa"),  # "F&S", "F/S"
    (("f", "o", "s"), "qa"),  # "F o S"
    (("q", "a"), "qa"),  # "Q&A"
    (("forfragningar", "under", "anbudstiden"), "qa"),
    (("change", "notice"), "amendment"),
    (("notice", "of", "modification"), "amendment"),
    (("meddelande", "till", "anbudsgivare"), "amendment"),
    (("meddelande", "till", "anbudsgivarna"), "amendment"),
    (("antagande", "av", "anbud"), "award"),
    (("beslut", "om", "tilldelning"), "award"),
    (("meddelande", "om", "tilldelning"), "award"),
    (("underrattelse", "om", "tilldelning"), "award"),
    (("sammanstallning", "av", "anbud"), "award"),
    (("contract", "award"), "award"),
    (("award", "notice"), "award"),
    (("award", "decision"), "award"),
    (("result", "notice"), "award"),
)

# Words that may stand next to a weak word without making the name an ordinary document: "Svar till anbudsgivare
# nr 2", "Tillägg 1 till förfrågningsunderlaget", "Ändring av sista anbudsdag". None of them holds a name alone.
_FILLER = frozenset(
    """
    och and nr no nummer number num till pa av om for fran the of to on in i med samt via ver version v rev utg
    utgava del sid pm bilaga bilagor anbudsgivare anbudsgivarna anbudsgivaren leverantor leverantorer leverantorerna
    inkomna inkommna under anbudstiden anbudstid upphandlingen upphandling forfragningsunderlag
    forfragningsunderlaget forfragningsunderlagen upphandlingsdokument upphandlingsdokumenten underlag underlaget
    sista anbudsdag anbudsdagen anbudsdatum datum dag tid anbudsfrist anbudsfristen information dokument dokumentet
    meddelande meddelanden besked beslut nya ny nytt uppdaterad uppdaterade uppdaterat reviderad reviderade
    reviderat slutlig slutligt final samlade samlad alla tender tenders document documents notice bidders
    januari februari mars april maj juni juli augusti september oktober november december
    january february march may june july august october
    """.split()
)

# "PM 1", "PM nr 2", "PM02": a numbered memo during the tender period is an amendment. A memo with a subject ("PM
# Geoteknik", "PM 01 Geoteknik") is a tender document and isn't matched.
_NUMBERED_MEMO = re.compile(r"(?:^| )pm(?: nr| nummer| no)? ?\d{1,3}(?: |$)")

_TOKEN = re.compile(r"[a-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-zåäöæøü]{2})(?=[A-ZÅÄÖÆØÜ][a-zåäöæøü])")


def fold(text: str) -> str:
    """Lower case, accents dropped, every run of other characters one space: 'Frågor & Svar_2' -> 'fragor svar 2'."""
    decomposed = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", text).casefold())
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(_TOKEN.findall(plain.replace("ß", "ss")))


def _split_glued(words: list[str]) -> list[str]:
    """Numbers glued to words count as separate words: 'fragor2' and '2fragor' -> 'fragor', '2'."""
    out = []
    for word in words:
        out += [w for w in re.split(r"(\d+)", word) if w]
    return out


def _words(text: str) -> list[str]:
    """Folded words, with CamelCase split ('FrågorOchSvar' -> 'fragor', 'och', 'svar') and numbers apart."""
    return _split_glued(fold(_CAMEL.sub(" ", unicodedata.normalize("NFC", text))).split())


def _strong(word: str) -> AnswerKind | None:
    if word in _STRONG:
        return _STRONG[word]
    for prefix, kind, unless in _PREFIXES:
        if word.startswith(prefix) and not word.startswith(unless):
            return kind
    for suffix, kind in _SUFFIXES:
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            return kind
    return None


def classify(text: str) -> AnswerKind | None:
    """The answer kind a name or heading points to, or None. Amendments win over Q&A, awards over both."""
    words = _words(text)
    found: set[AnswerKind] = {k for k in map(_strong, words) if k}
    for phrase, kind in _PHRASES:
        n = len(phrase)
        if any(tuple(words[i : i + n]) == phrase for i in range(len(words) - n + 1)):
            found.add(kind)
    pure = all(w.isdigit() or w in _FILLER or w in _WEAK or _strong(w) for w in words)
    if pure:
        found |= {_WEAK[w] for w in words if w in _WEAK}
        if _NUMBERED_MEMO.search(" ".join(words)):
            found.add("amendment")
    for kind in ("award", "amendment", "qa"):
        if kind in found:
            return kind  # type: ignore[return-value]
    return None


def classify_path(parts: tuple[str, ...] | list[str]) -> AnswerKind | None:
    """A file is held if its own name or any folder above it points to an answer kind. The file's extension is
    dropped first, so an extension can't match a word. A folder named after the tender ("Kompletterande markarbeten
    etapp 2") holds nothing: its weak word stands with ordinary words."""
    if not parts:
        return None
    *folders, name = parts
    stem = name.rsplit(".", 1)[0] if "." in name.strip(".") else name
    kinds = [k for k in (classify(p) for p in (*folders, stem)) if k]
    for kind in ("award", "amendment", "qa"):
        if kind in kinds:
            return kind  # type: ignore[return-value]
    return None
