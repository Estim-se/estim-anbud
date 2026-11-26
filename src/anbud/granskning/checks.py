"""The focused checks after the lenses, run together: zero and not-included calc rows, per-space rules, and outlets
per room type against the customer's recipe.

Each check runs on the cheap check role with the shared prefix (see `check_loop`). Their flags join the lenses'
flags before merge, so they are verified, priced and ranked like any other; their tables and coverage go into the
report's own sections. A check that fails or is cut short says so in coverage and never stops the run.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from anbud.granskning.check_loop import CheckRun
from anbud.granskning.contracts import CalcPost, LensResult
from anbud.granskning.corpus import Corpus
from anbud.granskning.outlet_recipe import Recipe, RecipeSection, check_outlet_recipe
from anbud.granskning.space_rules import SpaceRule, check_space_rules
from anbud.granskning.toolbox import Toolbox
from anbud.granskning.zero_rows import ZeroRow, check_zero_rows
from anbud.model_client import ModelClient
from anbud.models import Flag

TITLES = {
    "check_zero_rows": "Nollrader och ingår ej",
    "check_space_rules": "Regler per utrymme",
    "check_outlet_recipe": "Uttag per rumstyp",
}
_STATUS = {
    "skipped": "Hoppades över",
    "submitted": "Klar",
    "truncated": "Avbruten vid gränsen",
    "no_submit": "Inget svar",
    "error": "Fel",
}


class CheckCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    check: str
    title: str
    status: str
    turns: int = 0
    cost_usd: float = 0.0
    searches: list[str] = Field(default_factory=list)
    blocks_read: int = 0
    checked: str = Field(description="What the check covered, in Swedish")
    not_checked: list[str] = Field(default_factory=list)


class ChecksReport(BaseModel):
    """What the report shows for the checks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    zero_rows: list[ZeroRow] = Field(default_factory=list)
    space_rules: list[SpaceRule] = Field(default_factory=list)
    recipe: RecipeSection | None = None
    coverage: list[CheckCoverage] = Field(default_factory=list)
    cost_usd: float = 0.0
    skipped: str | None = Field(default=None, description="Why the checks didn't run at all, in Swedish")


def _coverage(run: CheckRun, checked: str) -> CheckCoverage:
    searches = [str(c.input.get("query", "")) for c in run.tool_log if c.tool == "search_blocks" and not c.refused]
    blocks = {b for c in run.tool_log if c.tool == "read_blocks" and not c.refused for b in c.result_ids}
    not_checked = []
    if run.skipped:
        not_checked.append(run.skipped)
    if run.status in ("error", "truncated", "no_submit"):
        errors = f" ({', '.join(sorted(set(run.errors)))})" if run.errors else ""
        not_checked.append(f"Kontrollen blev inte klar: {_STATUS[run.status].lower()}{errors}.")
    return CheckCoverage(
        check=run.name,
        title=TITLES.get(run.name, run.name),
        status=_STATUS.get(run.status, run.status),
        turns=run.turns,
        cost_usd=round(run.cost_usd, 4),
        searches=searches,
        blocks_read=len(blocks),
        checked=checked,
        not_checked=not_checked,
    )


def _zero_text(rows: Sequence[ZeroRow]) -> str:
    counts = {s: sum(1 for r in rows if r.status == s) for s in ("explained", "contradicted", "not_found")}
    return (
        f"{len(rows)} nollrader eller ingår ej-rader i kalkylen: {counts['explained']} förklarade, "
        f"{counts['contradicted']} motsägs av handlingarna, {counts['not_found']} utan hänvisning."
    )


def _space_text(rules: Sequence[SpaceRule]) -> str:
    short = sum(1 for r in rules if r.status in ("gap", "no_post"))
    unknown = sum(1 for r in rules if r.status == "unknown")
    text = f"{len(rules)} regler per utrymme hittade; {short} där kalkylen har för lite eller saknar post."
    if unknown:
        text += f" {unknown} där kalkylposten inte anger något antal; de prissätts inte."
    return text


def _recipe_text(section: RecipeSection) -> str:
    if section.status == "no_trigger":
        return "Ingen hänvisning till en standard för uttag per rumstyp hittades."
    if section.status == "trigger_unverified":
        return (
            "En hänvisning till en standard för uttag per rumstyp hittades, men dess citat kunde inte verifieras; "
            "ingen flagga ges. Kontrollera hänvisningen för hand."
        )
    if section.status == "failed":
        return "Kontrollen gav inget användbart svar."
    rows = sum(1 for r in section.rows if r.posts)
    extra = ""
    unmapped = sum(1 for r in section.rows if r.unmapped)
    if unmapped:
        extra += f" {unmapped} receptrader har ingen kalkylpost kopplad (ej kopplad, ej prissatt)."
    if section.rooms_unverified:
        extra += f" Antal rum per lägenhet inte styrkt för: {', '.join(section.rooms_unverified)} (ej prissatt)."
    if section.status == "no_recipe":
        ref = section.reference or "en standard"
        return f"FU hänvisar till {ref}; kundens recept saknas. {rows} receptrader.{extra}"
    short = sum(1 for r in section.rows if r.gap_per_room)
    return f"Jämfört med kundens recept ({section.recipe_source}): {rows} receptrader, {short} under receptet.{extra}"


class ChecksResult:
    def __init__(self, report: ChecksReport, flags: dict[str, list[Flag]], runs: list[CheckRun]) -> None:
        self.report = report
        self.flags = flags
        self.runs = runs

    @property
    def cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.runs)

    def lens_results(self, model: str) -> list[LensResult]:
        """The checks' flags as lens results, for merge (not for the lens coverage, which the checks have their own)."""
        return [
            LensResult(lens_id=run.name, model=model, status="submitted", flags=self.flags.get(run.name, []))
            for run in self.runs
            if self.flags.get(run.name)
        ]

    def summary(self) -> dict[str, Any]:
        """Ids, statuses, counts and costs for the run summary."""
        return {
            run.name: {
                "status": run.status,
                "turns": run.turns,
                "cost_usd": round(run.cost_usd, 4),
                "flags": len(self.flags.get(run.name, [])),
                "errors": run.errors,
            }
            for run in self.runs
        }


def not_run(reason: str) -> ChecksResult:
    """The checks' result when they don't run (skipped, or failed before they could), with the reason in coverage."""
    runs = [CheckRun(name) for name in TITLES]
    for run in runs:
        run.skipped = reason
    coverage = [_coverage(run, "Kontrollen kördes inte.") for run in runs]
    return ChecksResult(ChecksReport(coverage=coverage, skipped=reason), {}, runs)


def _safe(check: Callable[..., Any], name: str, empty: Any, *args: Any, **kwargs: Any) -> Any:
    """A check that raises (a bug, not a model error: the loop handles those) loses its own result, not the run's."""
    try:
        return check(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        run = CheckRun(name)
        run.skipped = f"Kontrollen avbröts av ett fel ({type(exc).__name__})."
        return empty, [], run


def run_checks(
    client: ModelClient,
    *,
    corpus: Corpus,
    calc: list[CalcPost],
    system: list[dict[str, Any]],
    toolbox: Toolbox,
    recipe: Recipe | None,
    lens_results: Sequence[LensResult] = (),
    concurrency: int = 3,
    on_progress: Callable[[str], None] | None = None,
) -> ChecksResult:
    say = on_progress or (lambda _msg: None)
    common = {"corpus": corpus, "calc": calc, "system": system, "toolbox": toolbox}
    with ThreadPoolExecutor(max_workers=max(1, min(3, concurrency))) as pool:
        zero = pool.submit(_safe, check_zero_rows, "check_zero_rows", [], client, **common)
        space = pool.submit(_safe, check_space_rules, "check_space_rules", [], client, **common)
        outlet = pool.submit(
            _safe,
            check_outlet_recipe,
            "check_outlet_recipe",
            RecipeSection(status="failed"),
            client,
            recipe=recipe,
            lens_results=lens_results,
            **common,
        )
        zero_rows, zero_flags, zero_run = zero.result()
        rules, space_flags, space_run = space.result()
        section, recipe_flags, recipe_run = outlet.result()
    runs = [zero_run, space_run, recipe_run]
    report = ChecksReport(
        zero_rows=zero_rows,
        space_rules=rules,
        recipe=section,
        coverage=[
            _coverage(zero_run, _zero_text(zero_rows)),
            _coverage(space_run, _space_text(rules)),
            _coverage(recipe_run, _recipe_text(section)),
        ],
        cost_usd=round(sum(r.cost_usd for r in runs), 4),
    )
    flags = {zero_run.name: zero_flags, space_run.name: space_flags, recipe_run.name: recipe_flags}
    for run in runs:
        say(f"check {run.name}: {run.status}, {len(flags[run.name])} flags, ${run.cost_usd:.2f}")
    return ChecksResult(report, flags, runs)
