"""The rule-first matcher: a run's flags against a variant's plants, and planted recall with both control arms.

A flag matches a plant by rule when all of these hold:
- **location:** in the arm the run read, it points where the plant is: its calc post (`money_basis.post_ref`, resolved
  through the run's own calc posts to a sheet and row), the plant's section, a block it cites, or (for a calc plant)
  text naming the plant's category;
- **money basis kind** is one the plant expects, with the same contract formula where the plant has one;
- **claim type** is one the plant expects;
- **direction and size** (§8.3), where both sides give a number: the calc is short, not over, and the quantity, hours
  or kronor are within a factor of 3 of the plant's.

A flag with the right location and only part of the rest goes to `judge`. Until the model judge is built it answers
None, and the pair is counted as unresolved: never as a catch.

Planted recall is reported on the plants both control arms miss (the structural detector and the calc-only run), by
count and kronor-weighted, per plant kind and per family, together with each control's catch share. The unplanted arm
is the false-positive baseline: flags there that match a plant at its unplanted location.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from anbud.evals.planted.categories import CATEGORIES
from anbud.evals.planted.key import ARMS, Arm, Key, Location, Plant
from anbud.granskning.contracts import CalcPost, fingerprint
from anbud.models import Flag

FACTOR = 3.0
TOO_EASY = 0.3
_STRENGTH = {"post": 4, "block": 3, "section": 2, "text": 1, "anywhere": 1}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Match(_Model):
    plant_id: str
    flag: int = Field(description="The flag's index in the run's flag list")
    fingerprint: str
    outcome: Literal["rule", "judge", "unresolved"]
    location: str
    basis_kind: bool
    claim_type: bool
    size: bool | None


Judge = Callable[[Plant, Flag], bool | None]


def judge_stub(plant: Plant, flag: Flag) -> bool | None:
    """The model judge's place. It decides nothing yet, so a partial match stays unresolved."""
    return None


def _within(value: float | None, expected: float | None) -> bool | None:
    if value is None or not expected:
        return None
    if value <= 0:
        return False  # the wrong direction: the flag says the calc is over
    ratio = value / abs(expected)
    return 1 / FACTOR <= ratio <= FACTOR


def _category_named(plant: Plant, flag: Flag) -> bool:
    if plant.family != "calc" or not plant.category:
        return False
    text = " ".join(filter(None, [flag.claim, flag.money_basis.item]))
    if plant.category.startswith("section:"):
        return plant.category.removeprefix("section:").casefold() in text.casefold()
    category = next((c for c in CATEGORIES if c.key == plant.category), None)
    return bool(category and category.matches(text))


def _location(plant: Plant, loc: Location, flag: Flag, post: CalcPost | None) -> str | None:
    if post is not None and any(p.sheet == post.sheet and p.row == post.row for p in loc.posts):
        return "post"
    if {c.block_id for c in flag.citations} & set(loc.blocks):
        return "block"
    if post is not None and post.section is not None and post.section in loc.sections:
        return "section"
    if _category_named(plant, flag):
        return "text"
    return "anywhere" if loc.anywhere else None


def _size(plant: Plant, flag: Flag, post: CalcPost | None, kronor: float | None) -> bool | None:
    basis = flag.money_basis
    expected = plant.expected
    per_post = None
    if post is not None:
        for change in plant.changes:
            if change.at and change.at.sheet == post.sheet and change.at.row == post.row and change.before is not None:
                per_post = change.before - (change.after or 0.0)
    checks = []
    if basis.kind == "quantity_gap" and basis.quantity is not None:
        checks.append(
            _within(basis.quantity, per_post if per_post and plant.kind == "cut_quantity" else expected.quantity)
        )
    if basis.kind == "hours_gap" and basis.hours is not None:
        checks.append(_within(basis.hours, per_post if per_post and plant.kind == "cut_hours" else expected.hours))
    checks.append(_within(kronor, expected.kronor))
    known = [c for c in checks if c is not None]
    return all(known) if known else None


def match_flag(
    plant: Plant, arm: Arm, flag: Flag, posts: dict[str, CalcPost], kronor: float | None = None
) -> tuple[str, bool, bool, bool | None] | None:
    """Location, basis-kind check, claim-type check and size check for one pair, or None if the location misses."""
    post = posts.get(flag.money_basis.post_ref or "")
    where = _location(plant, plant.locations[arm], flag, post)
    if where is None:
        return None
    expected = plant.expected
    kind_ok = flag.money_basis.kind in expected.basis_kinds and (
        expected.formula is None or flag.money_basis.formula == expected.formula
    )
    claim_ok = flag.claim_type in expected.claim_types
    return where, kind_ok, claim_ok, _size(plant, flag, post, kronor)


def match_arm(
    key: Key,
    arm: Arm,
    flags: list[Flag],
    calc_posts: list[CalcPost],
    kronor: dict[str, float] | None = None,
    judge: Judge = judge_stub,
) -> list[Match]:
    """Each flag to at most one plant: a rule match before a judge match before an unresolved one, then the
    strongest location, then the first plant."""
    posts = {p.id: p for p in calc_posts}
    out = []
    for i, flag in enumerate(flags):
        fp = fingerprint(flag)
        best: tuple[tuple[int, int, int], Match] | None = None
        for rank, plant in enumerate(key.plants):
            result = match_flag(plant, arm, flag, posts, (kronor or {}).get(fp))
            if result is None:
                continue
            where, kind_ok, claim_ok, size = result
            if kind_ok and claim_ok and size is not False:
                outcome: Literal["rule", "judge", "unresolved"] = "rule"
            elif kind_ok or claim_ok:
                verdict = judge(plant, flag)
                if verdict is False:
                    continue
                outcome = "judge" if verdict else "unresolved"
            else:
                continue
            order = {"rule": 2, "judge": 1, "unresolved": 0}[outcome]
            score = (order, _STRENGTH[where], -rank)
            match = Match(
                plant_id=plant.id,
                flag=i,
                fingerprint=fp,
                outcome=outcome,
                location=where,
                basis_kind=kind_ok,
                claim_type=claim_ok,
                size=size,
            )
            if best is None or score > best[0]:
                best = (score, match)
        if best is not None:
            out.append(best[1])
    return out


def structural_catches(key: Key) -> set[str]:
    """Plants control arm 1 caught: a finding on one of the plant's posts or in its section."""
    caught = set()
    for plant in key.plants:
        loc = plant.locations["planted"]
        rows = {(p.sheet, p.row) for p in loc.posts}
        for finding in key.structural:
            if (finding.sheet, finding.row) in rows or (finding.section and finding.section in loc.sections):
                caught.add(plant.id)
    return caught


class RunFlags(_Model):
    run_id: str
    flags: list[Flag]
    calc_posts: list[CalcPost] = Field(default_factory=list)
    kronor: dict[str, float] = Field(default_factory=dict)


class PlantScore(_Model):
    plant_id: str
    kind: str
    family: str
    category: str | None
    kronor: float | None
    structural: bool
    arms: dict[str, Literal["caught", "unresolved", "missed"]]


class Recall(_Model):
    plants: int
    caught: int
    unresolved: int
    recall: float | None
    kronor_plants: float
    kronor_caught: float
    kronor_recall: float | None


class Report(_Model):
    company_id: str
    tender_id: str
    seed: int
    runs: dict[str, str]
    plants: list[PlantScore]
    complete: bool = Field(description="Both control arms are in: the structural detector and a calc-only run")
    recall_clean: dict[str, Recall] = Field(description="On plants both controls miss: all, per family, per kind")
    recall_all: dict[str, Recall] = Field(description="On every plant, for reference")
    controls: dict[str, float | int | bool | None]
    baseline: dict[str, int]
    flags: dict[str, dict[str, int]]


def _status(matches: list[Match], plant_id: str) -> Literal["caught", "unresolved", "missed"]:
    outcomes = {m.outcome for m in matches if m.plant_id == plant_id}
    if outcomes & {"rule", "judge"}:
        return "caught"
    return "unresolved" if outcomes else "missed"


def _recall(scores: list[PlantScore]) -> Recall:
    caught = [s for s in scores if s.arms.get("planted") == "caught"]
    unresolved = [s for s in scores if s.arms.get("planted") == "unresolved"]
    k_all = sum(s.kronor or 0.0 for s in scores)
    k_caught = sum(s.kronor or 0.0 for s in caught)
    return Recall(
        plants=len(scores),
        caught=len(caught),
        unresolved=len(unresolved),
        recall=round(len(caught) / len(scores), 4) if scores else None,
        kronor_plants=round(k_all, 2),
        kronor_caught=round(k_caught, 2),
        kronor_recall=round(k_caught / k_all, 4) if k_all else None,
    )


def _grouped(scores: list[PlantScore]) -> dict[str, Recall]:
    out = {"all": _recall(scores)}
    for family in ("calc", "document"):
        out[f"family:{family}"] = _recall([s for s in scores if s.family == family])
    for kind in sorted({s.kind for s in scores}):
        out[f"kind:{kind}"] = _recall([s for s in scores if s.kind == kind])
    return out


def score(key: Key, runs: dict[Arm, RunFlags], judge: Judge = judge_stub) -> Report:
    matches = {
        arm: match_arm(key, arm, r.flags, r.calc_posts, r.kronor, judge) for arm, r in runs.items() if arm in ARMS
    }
    structural = structural_catches(key)
    scores = [
        PlantScore(
            plant_id=p.id,
            kind=p.kind,
            family=p.family,
            category=p.category,
            kronor=p.expected.kronor,
            structural=p.id in structural,
            arms={arm: _status(m, p.id) for arm, m in matches.items()},
        )
        for p in key.plants
    ]
    has_calc_only = "calc_only" in matches
    clean = [s for s in scores if not s.structural and not (has_calc_only and s.arms.get("calc_only") == "caught")]
    n = len(scores)
    calc_only_share = (
        round(sum(s.arms.get("calc_only") == "caught" for s in scores) / n, 4) if has_calc_only and n else None
    )
    baseline = matches.get("unplanted", [])
    return Report(
        company_id=key.company_id,
        tender_id=key.tender_id,
        seed=key.seed,
        runs={arm: r.run_id for arm, r in runs.items()},
        plants=scores,
        complete=has_calc_only,
        recall_clean=_grouped(clean),
        recall_all=_grouped(scores),
        controls={
            "structural_caught": len(structural),
            "structural_share": round(len(structural) / n, 4) if n else None,
            "calc_only_caught": sum(s.arms.get("calc_only") == "caught" for s in scores) if has_calc_only else None,
            "calc_only_share": calc_only_share,
            "calc_only_too_easy": calc_only_share > TOO_EASY if calc_only_share is not None else None,
            "structural_findings_planted": len(key.structural),
            "structural_findings_unplanted": len(key.structural_unplanted),
        },
        baseline={
            "flags": len(runs["unplanted"].flags) if "unplanted" in runs else 0,
            "flags_matching_a_plant": sum(m.outcome in ("rule", "judge") for m in baseline),
            "plants_hit": len({m.plant_id for m in baseline if m.outcome in ("rule", "judge")}),
        },
        flags={
            arm: {
                "total": len(runs[arm].flags),
                "matched": sum(m.outcome in ("rule", "judge") for m in ms),
                "unresolved": sum(m.outcome == "unresolved" for m in ms),
                "unmatched": len(runs[arm].flags) - len(ms),
            }
            for arm, ms in matches.items()
        },
    )
