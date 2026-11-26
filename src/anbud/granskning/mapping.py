"""The map step: Opus reads the shared prefix and submits a `TenderMap` through `submit_map`.

Code checks the map: it must parse, every cited block and every document it names must exist, and every lens it plans
must be a known lens. A failed check gets one repair turn. The map also writes the run's cache entry for the prefix.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from anbud.granskning.contracts import ToolCall
from anbud.granskning.corpus import Corpus
from anbud.granskning.lenses import Lens
from anbud.granskning.loop import LoopOutcome, SubmitOutcome, run_tool_loop
from anbud.granskning.toolbox import Toolbox
from anbud.model_client import ModelClient
from anbud.models import TenderMap

MAP_MAX_TOKENS = 32_000
MAP_MAX_USD = 2.0
MAP_MAX_TURNS = 6


def check_map(tender_map: TenderMap, corpus: Corpus, lens_ids: set[str]) -> list[str]:
    """Reasons the map fails code's checks; empty if it passes."""
    problems = []
    doc_ids = {d["doc_id"] for d in corpus.documents} | {b.doc_id for b in corpus.blocks.values()}
    for citation in tender_map.citations:
        if citation.block_id not in corpus.blocks:
            problems.append(f"citations: unknown block id {citation.block_id}")
    for doc in tender_map.documents:
        if doc.doc_id not in doc_ids:
            problems.append(f"documents: unknown document id {doc.doc_id}")
    seen: set[str] = set()
    for plan in tender_map.lenses:
        if plan.lens_id not in lens_ids:
            problems.append(f"lenses: unknown lens id {plan.lens_id}; known: {', '.join(sorted(lens_ids))}")
        if plan.lens_id in seen:
            problems.append(f"lenses: {plan.lens_id} is planned twice")
        seen.add(plan.lens_id)
    return problems


class MapSubmission:
    def __init__(self, corpus: Corpus, lens_ids: set[str]) -> None:
        self.corpus = corpus
        self.lens_ids = lens_ids
        self.tender_map: TenderMap | None = None
        self.problems: list[str] = []

    def __call__(self, raw: Any, tool_log: list[ToolCall], final: bool) -> SubmitOutcome:
        try:
            parsed = TenderMap.model_validate(raw)
        except ValidationError as exc:
            problems = [f"{'.'.join(map(str, e['loc'])) or 'map'}: {e['msg']}" for e in exc.errors()]
            parsed = None
        else:
            problems = check_map(parsed, self.corpus, self.lens_ids)
        if not problems:
            self.tender_map = parsed
            return SubmitOutcome(True, "Map received.")
        if final:
            self.problems = problems
            return SubmitOutcome(True, "Map received with problems; the run continues without it.")
        reasons = "\n".join(f"- {p}" for p in problems[:40])
        return SubmitOutcome(False, f"The map failed these checks. Fix them and call submit_map again:\n{reasons}")


def map_message(lenses: Sequence[Lens]) -> str:
    catalog = "\n".join(f"- {lens.id}: {lens.description}" for lens in lenses)
    return f"""\
<task>
Map this tender before the review lenses run. Read the corpus in the system prompt, use the read tools only where \
you need detail, then call submit_map once.

The map gives:
- documents: every document id in <documents> with its role (for example AF, administrativa föreskrifter, \
rambeskrivning, teknisk beskrivning el, drawing, room or object schedule, calc, other trade's description, \
gränsdragningslista, contract) and revision if stated;
- contract_form (the standard general terms and subcontract terms that apply, by name, or the client's own), \
building_type, \
occupancy (empty building, tenants in place, premises in operation), stated_scope (two or three sentences), \
stage_plan, permit_date if stated;
- documents_named_not_present: documents the package names or relies on that are not in it;
- lenses: one entry for every lens below that has anything to look at in this tender, each with 3 to 6 focus \
questions specific to this tender (what to check and where it likely sits, by block id or document);
- citations: the blocks behind the contract form, scope, stages and permit date, each with an exact short quote.

Lenses:
{catalog}

Cite only block ids that appear in the corpus. Write stated_scope, stage_plan and focus questions in Swedish.
</task>"""


@dataclass
class MapResult:
    tender_map: TenderMap | None
    outcome: LoopOutcome
    problems: list[str]

    @property
    def prefix_tokens(self) -> int | None:
        """The shared prefix's size, read off the map's first call: what it wrote to or read from the cache."""
        usage = self.outcome.first_usage
        if usage is None:
            return None
        written = getattr(usage, "cache_creation_input_tokens", None) or 0
        read = getattr(usage, "cache_read_input_tokens", None) or 0
        return (written + read) or None


def run_map(
    client: ModelClient,
    *,
    system: list[dict[str, Any]],
    corpus: Corpus,
    toolbox: Toolbox,
    lenses: Sequence[Lens],
    max_turns: int = MAP_MAX_TURNS,
    max_usd: float = MAP_MAX_USD,
    effort: str = "high",
) -> MapResult:
    submission = MapSubmission(corpus, {lens.id for lens in lenses})
    outcome = run_tool_loop(
        client,
        role="map",
        step="map",
        system=system,
        first_message=map_message(lenses),
        toolbox=toolbox,
        submit_tool="submit_map",
        on_submit=submission,
        max_turns=max_turns,
        max_usd=max_usd,
        max_tokens=MAP_MAX_TOKENS,
        effort=effort,
    )
    return MapResult(submission.tender_map, outcome, submission.problems)
