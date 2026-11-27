"""Which documents of a tender the models read: the electrical trade's own and the common ones, not other trades'.

General Swedish tender practice, from no tender: a full tender set holds every trade's documents (architect,
structure, plumbing, ventilation, groundworks and more), and an electrical contractor prices from the electrical
documents and the common ones (administrative terms, contract, general description, time plan, lists of rooms and
objects, boundary lists). Other trades' documents still matter where they refer to the electrical work: a pump the
electrical contractor connects, a boundary between contracts.

- `classify` reads one document's discipline from generic signals: the discipline letter that leads Swedish drawing
  and document names (common practice after Bygghandlingar 90 and SB11: A architect, K structure, R plumbing,
  V ventilation, M groundworks, L landscape, G geotechnics, E electrical, T telecom); trade words in its file and
  folder names and in its first page's opening text; and the BSAB system class of its numbered code headings (5x pipe
  and air systems, 6x electrical and telecom systems, 7x transport systems), counted by code number only.
- It fails open. A document leaves the models' input only on a positive other-trade signal and no electrical or
  common one. Anything unclear is read.
- `select` decides for the tender. An architect's document leaves only when the tender has electrical documents of
  its own, since without them the architect's plans are what the electrical work is counted from. A tender where
  every document looks like another trade's is read whole. A local override file moves documents either way.
- Excluded documents stay in the corpus and its search index (`Corpus.all_order`). The lines in them that refer to
  the electrical work come back into the models' input with their headings, as cited excerpts (`Selection.boundary`).

`classify` is a pure function of a `DocumentView` with a stable interface, so the exam's aggregate size report uses
the same classifier. Signals are generic names ("name:K", "word:V", "codes:5x"), never a file name or tender text.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path, PurePath
from typing import Any, Literal

from anbud.granskning.corpus import Corpus
from anbud.models import Block

Discipline = Literal["el", "common", "other", "unknown"]

OVERRIDE_FILE = "selection.toml"  # in the tender's folder: `read = [doc ids]`, `exclude = [doc ids]`
SELECTION_FILE = "selection.json"
MANUAL = "manuellt"  # the trade of a document the override excludes
ARCHITECT = "A"
HEADER_BLOCKS = 10  # the first page's opening blocks: title block and document title
HEADER_CHARS = 600

# Estim's short name of each trade, the letter that leads its drawing and document names, and the stems of its
# trade words. A trade word is a stem alone or a stem joined to a document word ("elritningar", "ventilationsplan").
TRADES: tuple[tuple[str, Discipline, str | None, tuple[str, ...]], ...] = (
    ("El", "el", "E", ("el", "elkraft", "starkström", "svagström", "belysning", "brandlarm", "elteknik")),
    ("Tele", "el", "T", ("tele", "telekommunikation")),
    ("VS", "other", "R", ("vs", "vvs", "rör", "sanitet", "värme", "kyl")),
    ("V", "other", "V", ("ventilation", "vent", "luftbehandling")),
    ("K", "other", "K", ("konstruktion", "stom", "armering")),
    (ARCHITECT, "other", "A", ("arkitekt",)),
    ("M", "other", "M", ("mark",)),
    ("L", "other", "L", ("landskap",)),
    ("G", "other", "G", ("geoteknik", "geo")),
    ("Sprinkler", "other", None, ("sprinkler",)),
    ("Hiss", "other", None, ("hiss",)),
)
_DOC_WORDS = (
    "ritning",
    "ritningar",
    "handling",
    "handlingar",
    "beskrivning",
    "beskrivningar",
    "installation",
    "installationer",
    "arbete",
    "arbeten",
    "plan",
    "planer",
    "system",
    "teknik",
    "förteckning",
    "underlag",
    "entreprenad",
    "projektering",
)
# Document words that make a document common to every trade, matched at the start of a word.
COMMON = (
    "administrativ",
    "kontrakt",
    "rambeskrivning",
    "tidplan",
    "anbudsformulär",
    "anbudsförfrågan",
    "gränsdragning",
    "ansvarsfördelning",
    "ritningsförteckning",
    "handlingsförteckning",
    "lägenhetsförteckning",
    "rumsförteckning",
    "rumsbeskrivning",
    "rumsfunktion",
    "brandskydd",
    "arbetsmiljö",
    "miljöplan",
)
_COMMON_EXACT = ("af",)  # the administrative terms' own abbreviation, as a whole word
_TOKEN = re.compile(r"[a-zåäöéü0-9]+")
_NAME_LETTER = re.compile(r"^([A-Z])(?:[-_ .]?\d)")
_SYSTEM_CODE = re.compile(r"^([5-7])\d(?:\.|$)")
_AF_CODE = re.compile(r"^AF[A-G]\b")
SYSTEM_CLASSES = {"5": "VS", "6": "El", "7": "Hiss"}  # BSAB 96 system classes; 5x covers pipe and air alike
MIN_CODES = 3

# A line in another trade's document that refers to the electrical work: the electrical contractor, electrical
# connections and feeds, telecom, a contract boundary. Estim's own generic words.
EL_REFERENCE = re.compile(
    r"(?<![a-zåäö])(?:el(?:entreprenör|arbete|anslut|matning|installat|central|kabel|kraft|ritning|uttag|-)\w*|el"
    r"|e-?entreprenör\w*|elektrisk\w*|(?:kraft)?matning\w*|starkström\w*|tele\w*|gränsdragning\w*)(?![a-zåäö])"
)


class SelectionError(RuntimeError):
    """Raised with a reason that names no tender content."""


@dataclass(frozen=True)
class DocumentView:
    """What `classify` reads of one document. `paths` are relative to the inbox, folders included: local only."""

    doc_id: str
    paths: tuple[str, ...] = ()
    pages: int = 0
    header: str = ""  # the first page's opening text
    heading_codes: tuple[str, ...] = ()  # the code number leading each heading, such as "63.B" or "AFC.1"


@dataclass(frozen=True)
class Classification:
    discipline: Discipline
    trade: str | None = None  # Estim's short name of the trade the signals point to
    signals: tuple[str, ...] = ()  # generic signal names, safe to log

    @property
    def read(self) -> bool:
        return self.discipline != "other"


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _is_word(token: str, stem: str) -> bool:
    if token == stem:
        return True
    if not token.startswith(stem):
        return False
    rest = token[len(stem) :]
    rest = rest[1:] if rest.startswith("s") and rest[1:] in _DOC_WORDS else rest
    return rest in _DOC_WORDS


def _trade_words(tokens: list[str]) -> list[str]:
    return [trade for trade, _, _, stems in TRADES if any(_is_word(t, s) for t in tokens for s in stems)]


def _common_words(tokens: list[str]) -> bool:
    return any(t in _COMMON_EXACT or t.startswith(COMMON) for t in tokens)


def classify(view: DocumentView) -> Classification:
    """One document's discipline from its names, folders, opening text and code headings. Pure and deterministic."""
    signals: list[str] = []
    trades: list[str] = []
    common = False
    letters = {trade: letter for trade, _, letter, _ in TRADES if letter}
    for path in view.paths:
        parts = PurePath(path).parts
        stem = PurePath(path).stem
        m = _NAME_LETTER.match(stem)
        if m:
            for trade, letter in letters.items():
                if letter == m.group(1):
                    signals.append(f"name:{letter}")
                    trades.append(trade)
        for where, text in (("name", stem), *(("folder", p) for p in parts[:-1])):
            tokens = _tokens(text)
            for trade in _trade_words(tokens):
                signals.append(f"{where}:{trade}")
                trades.append(trade)
            if _common_words(tokens):
                signals.append(f"{where}:common")
                common = True
    header = _tokens(view.header)
    for trade in _trade_words(header):
        signals.append(f"header:{trade}")
        trades.append(trade)
    if _common_words(header):
        signals.append("header:common")
        common = True
    if sum(1 for c in view.heading_codes if _AF_CODE.match(c)) >= MIN_CODES:
        signals.append("codes:AF")
        common = True
    classes = Counter(m.group(1) for c in view.heading_codes if (m := _SYSTEM_CODE.match(c)))
    if sum(classes.values()) >= MIN_CODES:
        top, n = classes.most_common(1)[0]
        if n * 3 >= sum(classes.values()) * 2:
            signals.append(f"codes:{top}x")
            trades.append(SYSTEM_CLASSES[top])
    kinds = {trade: kind for trade, kind, _, _ in TRADES}
    found = tuple(dict.fromkeys(signals))
    if any(kinds.get(t) == "el" for t in trades):
        return Classification("el", None, found)
    if common:
        return Classification("common", None, found)
    others = Counter(t for t in trades if kinds.get(t) == "other")
    if others:
        best = max(others.items(), key=lambda kv: (kv[1], -list(kinds).index(kv[0])))[0]
        return Classification("other", best, found)
    return Classification("unknown", None, found)


@dataclass(frozen=True)
class Selection:
    classes: dict[str, Classification]  # doc_id -> classification
    excluded: dict[str, str]  # doc_id -> trade, for documents left out of the models' input
    pages: dict[str, int]  # doc_id -> pages, for every document
    boundary: tuple[str, ...] = ()  # block ids brought back from excluded documents: lines and their headings
    boundary_lines: int = 0  # the lines among them, headings not counted
    overridden: tuple[str, ...] = ()  # doc ids the override moved
    override_sha256: str | None = None

    @property
    def pages_excluded(self) -> int:
        return sum(self.pages.get(d, 0) for d in self.excluded)

    def excluded_pages_by_trade(self) -> dict[str, int]:
        out: Counter[str] = Counter()
        for doc_id, trade in self.excluded.items():
            out[trade] += self.pages.get(doc_id, 0)
        return dict(sorted(out.items()))

    def summary(self) -> dict[str, Any]:
        """Counts only: safe to print and to log."""
        return {
            "documents_by_discipline": dict(sorted(Counter(c.discipline for c in self.classes.values()).items())),
            "documents_excluded": len(self.excluded),
            "pages_excluded": self.pages_excluded,
            "pages_excluded_by_trade": self.excluded_pages_by_trade(),
            "boundary_lines": self.boundary_lines,
            "overridden": len(self.overridden),
        }

    def to_json(self) -> dict[str, Any]:
        """Doc ids, classes, signals and pages: no file name, no tender text."""
        return {
            "documents": [
                {
                    "doc_id": doc_id,
                    "discipline": c.discipline,
                    "trade": c.trade,
                    "signals": list(c.signals),
                    "read": doc_id not in self.excluded,
                    "pages": self.pages.get(doc_id, 0),
                }
                for doc_id, c in sorted(self.classes.items())
            ],
            "boundary": list(self.boundary),
            "overridden": list(self.overridden),
            "override_sha256": self.override_sha256,
            **self.summary(),
        }


def heading_code(text: str) -> str | None:
    """The code number leading a heading, such as "63.B", "AFC.1" or "PNB.12", or None."""
    m = re.match(r"^(?:[A-ZÅÄÖ]{1,4}(?:\.\d+)*|\d{2,3}(?:\.[A-ZÅÄÖ]{1,4}(?:/\d+)?)?)(?=\s|$)", text.strip())
    return m.group(0) if m else None


def views(corpus: Corpus) -> list[DocumentView]:
    """One view per document prep recorded, and per document that has blocks but no record."""
    by_doc: dict[str, list[Block]] = {}
    for block_id in corpus.all_order:
        block = corpus.blocks[block_id]
        by_doc.setdefault(block.doc_id, []).append(block)
    docs = {d["doc_id"]: d for d in corpus.documents}
    order = list(docs) + [d for d in by_doc if d not in docs]
    out = []
    for doc_id in order:
        doc = docs.get(doc_id, {})
        blocks = by_doc.get(doc_id, [])
        first_page = min((b.page for b in blocks if b.page is not None), default=None)
        opening = [b.text for b in blocks if b.page == first_page][:HEADER_BLOCKS] if blocks else []
        codes = [e.number or heading_code(e.title) for e in corpus.outline if e.doc_id == doc_id]
        codes += [heading_code(b.text) for b in blocks if b.kind == "heading"]
        out.append(
            DocumentView(
                doc_id=doc_id,
                paths=tuple(str(p) for p in doc.get("paths") or ()),
                pages=len(doc.get("pages") or ()),
                header=" ".join(opening)[:HEADER_CHARS],
                heading_codes=tuple(c for c in codes if c),
            )
        )
    return out


def load_override(path: Path | None, known: set[str]) -> tuple[set[str], set[str], str | None]:
    if path is None or not path.is_file():
        return set(), set(), None
    raw = path.read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise SelectionError("the selection override isn't valid TOML") from exc
    read, exclude = set(data.get("read", [])), set(data.get("exclude", []))
    if not all(isinstance(d, str) for d in read | exclude):
        raise SelectionError("the selection override lists something that isn't a document id")
    if (read | exclude) - known:
        raise SelectionError(f"the selection override names {len((read | exclude) - known)} unknown document ids")
    if read & exclude:
        raise SelectionError("the selection override both reads and excludes a document")
    return read, exclude, hashlib.sha256(raw).hexdigest()


def _boundary(corpus: Corpus, excluded: set[str]) -> tuple[list[str], int]:
    """Lines in excluded documents that refer to the electrical work, each with the headings above it."""
    levels = {e.block_id: e.level for e in corpus.outline if e.block_id}
    keep: list[str] = []
    lines = 0
    path: list[tuple[int, str]] = []
    doc = None
    for block_id in corpus.all_order:
        block = corpus.blocks[block_id]
        if block.doc_id != doc:
            doc, path = block.doc_id, []
        if block.doc_id not in excluded:
            continue
        if block.kind == "heading":
            level = levels.get(block_id, 1)
            path = [(lv, b) for lv, b in path if lv < level] + [(level, block_id)]
            continue
        if EL_REFERENCE.search(block.text.lower()):
            keep.extend(b for _, b in path)
            keep.append(block_id)
            lines += 1
    return list(dict.fromkeys(keep)), lines


def select(corpus: Corpus, override: Path | None = None) -> Selection:
    """The tender's selection: each document's class, the documents left out of the models' input, and the lines
    brought back from them."""
    vs = views(corpus)
    classes = {v.doc_id: classify(v) for v in vs}
    pages = {v.doc_id: v.pages for v in vs}
    read, exclude, sha = load_override(override, set(classes))
    has_el = any(c.discipline == "el" for c in classes.values())
    excluded = {
        d: c.trade or "?" for d, c in classes.items() if c.discipline == "other" and (c.trade != ARCHITECT or has_el)
    }
    if excluded and len(excluded) == len(classes):
        excluded = {}  # every document looks like another trade's: the signals are wrong, read it whole
    excluded = {d: t for d, t in excluded.items() if d not in read} | {d: MANUAL for d in sorted(exclude)}
    moved = tuple(sorted(read | exclude))
    boundary, lines = _boundary(corpus, set(excluded))
    return Selection(classes, dict(sorted(excluded.items())), pages, tuple(boundary), lines, moved, sha)


def apply(corpus: Corpus, selection: Selection) -> Corpus:
    """The corpus the models read: excluded documents' blocks leave `order` (boundary lines stay, in place) and their
    tiles leave `tiles`; every block stays in `blocks` and `all_order`, so search and lookups still reach them."""
    if not selection.excluded:
        return replace(corpus, excluded={})
    keep = set(selection.boundary)
    order = [b for b in corpus.all_order if corpus.blocks[b].doc_id not in selection.excluded or b in keep]
    tiles = {k: t for k, t in corpus.tiles.items() if t.doc_id not in selection.excluded}
    all_order = list(corpus.all_order)
    return replace(corpus, order=order, tiles=tiles, all_order=all_order, excluded=dict(selection.excluded))


def load_selected(prep_dir: Path, override: Path | None = None) -> tuple[Corpus, Selection]:
    """The prepared corpus with the selection applied. The override defaults to the tender folder's file."""
    corpus = Corpus.load(prep_dir)
    selection = select(corpus, override if override is not None else prep_dir.parent / OVERRIDE_FILE)
    return apply(corpus, selection), selection


def excluded_counts(corpus: Corpus) -> dict[str, Any]:
    """What the models didn't read, from the corpus alone: counts only, safe to print and to log."""
    pages = {d["doc_id"]: len(d.get("pages") or ()) for d in corpus.documents}
    by_trade: Counter[str] = Counter()
    for doc_id, trade in corpus.excluded.items():
        by_trade[trade] += pages.get(doc_id, 0)
    lines = sum(
        1 for b in corpus.order if corpus.blocks[b].doc_id in corpus.excluded and corpus.blocks[b].kind != "heading"
    )
    return {
        "documents_excluded": len(corpus.excluded),
        "pages_excluded": sum(by_trade.values()),
        "pages_excluded_by_trade": dict(sorted(by_trade.items())),
        "boundary_lines": lines,
    }


def excluded_label(corpus: Corpus) -> str:
    """ "N sidor, M dokument", with the pages per trade."""
    c = excluded_counts(corpus)
    by_trade = ", ".join(f"{t} {n}" for t, n in c["pages_excluded_by_trade"].items())
    head = f"{c['pages_excluded']} sidor, {c['documents_excluded']} dokument"
    return f"{head} ({by_trade})" if by_trade else head
