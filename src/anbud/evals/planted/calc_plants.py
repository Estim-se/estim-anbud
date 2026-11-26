"""Natural plants on typed calc posts, applied before the calc is regenerated.

Each plant takes a whole category the way an estimator misses it, never a single row on its own:
- `remove_category`: every post in the category goes, with its sibling notes; a post that only mentions the category
  in an "inkl." or "samt" clause loses the clause and stays;
- `cut_quantity`: every quantity in the category is cut by the same share (hours and lump amounts follow, so the
  hours per unit stay at the calc's own norm);
- `cut_hours`: every hours figure in the category is cut by the same share.

Plants never touch the same post twice. A calc whose wording fits no category list is planted by section instead.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from random import Random

from anbud.evals.planted.calc import amount_of, priced
from anbud.evals.planted.categories import CATEGORIES, Category, members, section_category
from anbud.evals.planted.key import PlantKind
from anbud.granskning.contracts import CalcPost

MIN_SHARE, MAX_SHARE = 0.3, 0.5
_CLAUSE = re.compile(r"[,;]?\s*\b(?:inkl\.?|inklusive|samt)\s+[^,;()]*", re.IGNORECASE)


@dataclass
class CalcEdit:
    kind: PlantKind
    category: Category
    removed: list[int] = field(default_factory=list)
    edited: dict[int, CalcPost] = field(default_factory=dict)
    actions: dict[int, str] = field(default_factory=dict)

    def touched(self) -> set[int]:
        return set(self.removed) | set(self.edited)

    def kronor(self, posts: list[CalcPost]) -> float | None:
        if self.kind == "cut_hours":
            return None
        lost = sum(amount_of(posts[i]) or 0.0 for i in self.removed)
        lost += sum((amount_of(posts[i]) or 0.0) - (amount_of(new) or 0.0) for i, new in self.edited.items())
        return round(lost, 2) if lost > 0 else None

    def quantity(self, posts: list[CalcPost]) -> float | None:
        if self.kind != "cut_quantity":
            return None
        return round(sum((posts[i].quantity or 0.0) - (new.quantity or 0.0) for i, new in self.edited.items()), 3)

    def hours(self, posts: list[CalcPost]) -> float | None:
        if self.kind == "remove_category":
            lost = sum(posts[i].hours or 0.0 for i in self.removed)
        else:
            lost = sum((posts[i].hours or 0.0) - (new.hours or 0.0) for i, new in self.edited.items())
        return round(lost, 3) if lost > 0 else None


def _strip_mention(text: str, category: Category) -> str | None:
    """The text without the inclusion clauses that name the category, if what is left no longer belongs to it."""
    clauses = [m for m in _CLAUSE.finditer(text) if category.matches(m.group(0))]
    if not clauses:
        return None
    kept = text
    for m in reversed(clauses):
        kept = kept[: m.start()] + kept[m.end() :]
    kept = kept.strip(" ,;")
    return kept if len(kept) >= 3 and not category.matches(kept) else None


def _share(rng: Random) -> float:
    return round(rng.uniform(MIN_SHARE, MAX_SHARE), 2)


def _remove(category: Category, posts: list[CalcPost], rng: Random) -> CalcEdit | None:
    idx = members(category, posts)
    edit = CalcEdit("remove_category", category)
    for i in idx:
        stripped = None if category.key.startswith("section:") else _strip_mention(posts[i].text, category)
        if stripped is not None:
            edit.edited[i] = posts[i].model_copy(update={"text": stripped})
            edit.actions[i] = "mention_removed"
        else:
            edit.removed.append(i)
            edit.actions[i] = "removed"
    if len(edit.removed) < 2 or not any((amount_of(posts[i]) or 0) > 0 for i in edit.removed):
        return None
    return edit


def _cut_quantity(category: Category, posts: list[CalcPost], rng: Random) -> CalcEdit | None:
    share = _share(rng)
    edit = CalcEdit("cut_quantity", category)
    for i in members(category, posts):
        post = posts[i]
        q = post.quantity
        if not priced(post) or q is None or q <= 0:
            continue
        new = math.floor(q * (1 - share)) if float(q).is_integer() else round(q * (1 - share), 1)
        if new >= q or new < (1 if float(q).is_integer() else 0.1):
            continue
        ratio = new / q
        update: dict[str, float | None] = {"quantity": float(new)}
        if post.hours is not None:
            update["hours"] = round(post.hours * ratio, 2)
        if post.unit_price is None and post.amount is not None:
            update["amount"] = round(post.amount * ratio, 2)
        edit.edited[i] = post.model_copy(update=update)
        edit.actions[i] = "quantity_cut"
    return edit if edit.edited else None


def _cut_hours(category: Category, posts: list[CalcPost], rng: Random) -> CalcEdit | None:
    share = _share(rng)
    edit = CalcEdit("cut_hours", category)
    for i in members(category, posts):
        h = posts[i].hours
        if h is None or h <= 0:
            continue
        new = round(h * (1 - share), 1)
        if new <= 0 or new >= h:
            continue
        edit.edited[i] = posts[i].model_copy(update={"hours": new})
        edit.actions[i] = "hours_cut"
    return edit if edit.edited else None


_BUILDERS = {"remove_category": _remove, "cut_quantity": _cut_quantity, "cut_hours": _cut_hours}


def candidates(kind: PlantKind, posts: list[CalcPost], used: set[int], rng: Random) -> list[CalcEdit]:
    """Every plant of this kind the calc allows without touching a used post: keyword categories, else sections."""
    found = []
    for category in CATEGORIES:
        edit = _BUILDERS[kind](category, posts, Random(rng.random()))
        if edit and not edit.touched() & used:
            found.append(edit)
    if found:
        return found
    sections = sorted({p.section for p in posts if p.section})
    for title in sections:
        edit = _BUILDERS[kind](section_category(title), posts, Random(rng.random()))
        if edit and not edit.touched() & used:
            found.append(edit)
    return found


def plant_calc(posts: list[CalcPost], rng: Random, n: int) -> list[CalcEdit]:
    """Up to n calc plants on disjoint posts, cycling through the kinds in a seeded order."""
    kinds: list[PlantKind] = ["remove_category", "cut_quantity", "cut_hours"]
    rng.shuffle(kinds)
    edits: list[CalcEdit] = []
    used: set[int] = set()
    used_categories: set[str] = set()
    stalled = 0
    k = 0
    while len(edits) < n and stalled < len(kinds):
        kind = kinds[k % len(kinds)]
        k += 1
        options = [e for e in candidates(kind, posts, used, rng) if e.category.key not in used_categories]
        if not options:
            stalled += 1
            continue
        stalled = 0
        edit = options[rng.randrange(len(options))]
        edits.append(edit)
        used |= edit.touched()
        used_categories.add(edit.category.key)
    return edits


def apply(posts: list[CalcPost], edits: list[CalcEdit]) -> tuple[list[CalcPost], dict[int, int]]:
    """The planted posts, and each surviving source index's index among them."""
    removed = {i for e in edits for i in e.removed}
    edited = {i: p for e in edits for i, p in e.edited.items()}
    out: list[CalcPost] = []
    where: dict[int, int] = {}
    for i, post in enumerate(posts):
        if i in removed:
            continue
        where[i] = len(out)
        out.append(edited.get(i, post))
    return out, where
