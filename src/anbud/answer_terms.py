"""Words that mark a file, a folder or a section as part of an answer key.

The answer keys for a public tender are the buyer's own questions and answers, its amendments, and the award decision
and opening protocol. They must never reach a run as input: intake moves matching files to `answers/<co>/<t>/` before
prep, and prep withholds matching sections of a notice page. One rule serves both, so they can't disagree.

Matching is on whole words after folding (case, accents, punctuation), so a word that only contains a term doesn't
match: the Swedish word for a request for tenders contains the word for "question" and must not be caught.

When in doubt a term is listed: a document wrongly held back costs a little input, an answer that slips through
spoils the blind score. Every hold is counted, and the local manifest lists what was held, so a dev tender's holds
can be checked once its first run is frozen.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

AnswerKind = Literal["qa", "amendment", "award"]

# Single words, after folding. Compound words are listed whole: matching is never on parts of words.
_WORDS: dict[str, AnswerKind] = {
    # Questions and answers
    **dict.fromkeys(
        (
            "fraga", "fragan", "fragor", "fragorna", "fragesvar", "fragorsvar", "fragestallning", "fragestallningar",
            "svar", "svaren", "fos", "qa", "questions", "question", "answers", "clarification", "clarifications",
        ),
        "qa",
    ),
    # Amendments and corrections
    **dict.fromkeys(
        (
            "andring", "andringar", "andringen", "andringarna", "andringsmeddelande", "andringsannons",
            "andringsmeddelanden", "rattelse", "rattelser", "rattelsen", "rattelsemeddelande", "komplettering",
            "kompletteringar", "kompletteringen", "kompletteringsbrev", "kompletteringspm", "kompletterande", "kfu",
            "fortydligande", "fortydliganden", "fortydligandet", "tillagg", "tillaggsinformation", "tillaggsbrev",
            "corrigendum", "addendum", "amendment", "amendments", "erratum", "errata",
        ),
        "amendment",
    ),
    # Award decisions, opening protocols and evaluation results
    **dict.fromkeys(
        (
            "tilldelning", "tilldelningsbeslut", "tilldelningsbeslutet", "tilldelningsmeddelande", "tilldelningen",
            "oppningsprotokoll", "anbudsoppning", "anbudsoppningsprotokoll", "anbudsoppningen", "protokoll",
            "protokollet", "utvarderingsrapport", "utvarderingsprotokoll", "upphandlingsrapport",
            "upphandlingsprotokoll", "avtalsbeslut", "kontraktstilldelning", "antagandebesked", "anbudssammanstallning",
            "award", "awarded", "tabulation",
        ),
        "award",
    ),
}  # fmt: skip

# Short phrases, as folded word sequences: initials and two-word terms that are too loose alone.
_PHRASES: tuple[tuple[tuple[str, ...], AnswerKind], ...] = (
    (("f", "s"), "qa"),  # "F&S", "F/S"
    (("q", "a"), "qa"),  # "Q&A"
    (("antagande", "av", "anbud"), "award"),
    (("beslut", "om", "tilldelning"), "award"),
    (("sammanstallning", "av", "anbud"), "award"),
)

# "PM 1", "PM nr 2", "PM02": a numbered memo during the tender period is an amendment. A memo with a subject and no
# number ("PM Geoteknik") is a tender document and isn't matched.
_NUMBERED_MEMO = re.compile(r"(?:^| )pm(?: nr)? ?\d{1,3}(?: |$)")

_TOKEN = re.compile(r"[a-z0-9]+")


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


def classify(text: str) -> AnswerKind | None:
    """The answer kind a name or heading points to, or None. Amendments win over Q&A, awards over both."""
    folded = fold(text)
    words = _split_glued(folded.split())
    found: set[AnswerKind] = {_WORDS[w] for w in words if w in _WORDS}
    for phrase, kind in _PHRASES:
        n = len(phrase)
        if any(tuple(words[i : i + n]) == phrase for i in range(len(words) - n + 1)):
            found.add(kind)
    if _NUMBERED_MEMO.search(" ".join(words)):
        found.add("amendment")
    for kind in ("award", "amendment", "qa"):
        if kind in found:
            return kind  # type: ignore[return-value]
    return None


def classify_path(parts: tuple[str, ...] | list[str]) -> AnswerKind | None:
    """A file is held if its own name or any folder above it points to an answer kind. The file's extension is
    dropped first, so an extension can't match a word."""
    if not parts:
        return None
    *folders, name = parts
    stem = name.rsplit(".", 1)[0] if "." in name.strip(".") else name
    kinds = [k for k in (classify(p) for p in (*folders, stem)) if k]
    for kind in ("award", "amendment", "qa"):
        if kind in kinds:
            return kind  # type: ignore[return-value]
    return None
