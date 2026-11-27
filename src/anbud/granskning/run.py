"""Granskning orchestration, up to the lenses: prefix, map, then every lens in parallel, all through one ModelClient.

Each step appends events (ids, counts, timings and costs only). The map's and each lens's full result go into the
company's blob store, and `runs/<run_id>/granskning.json` lists their blob hashes with statuses, counts and costs, so
the later stages (checks, merge, verifier, kronor, report) can load them. Every lens runs, whether or not the map
planned it; the map's focus questions go to the lenses it planned.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from anbud.granskning.contracts import CalcPost, LensResult
from anbud.granskning.corpus import Corpus
from anbud.granskning.lenses import Lens
from anbud.granskning.loop import EFFORT, LENS_MAX_TOKENS, Validator, pydantic_validator, run_lens
from anbud.granskning.mapping import MapResult, run_map
from anbud.granskning.prefix import CHARS_PER_TOKEN, defuse, estimate_tokens, prefix_chars, system_blocks
from anbud.granskning.toolbox import Toolbox
from anbud.model_client import ModelClient
from anbud.models import Event, TenderMap
from anbud.selection import excluded_counts
from anbud.store import canonical

DEFAULT_CONCURRENCY = 3
# A map that fails with one of these stops the run before the lenses: they would fail the same way.
FATAL_ERRORS = ("RunStopped", "BudgetExceeded", "spend_cap", "ReplayMiss")


@dataclass(frozen=True)
class LensCaps:
    max_turns: int
    max_usd: float


def lens_caps(budgets: dict[str, Any], prefix_tokens: int) -> LensCaps:
    """Turns and dollars per lens, from the tier in config/budgets.toml that the prefix size falls in."""
    tiers = sorted(budgets["lens_tiers"], key=lambda t: t["max_prefix_tokens"])
    for tier in tiers:
        if prefix_tokens <= tier["max_prefix_tokens"]:
            return LensCaps(int(tier["max_turns"]), float(tier["max_usd"]))
    return LensCaps(int(tiers[-1]["max_turns"]), float(tiers[-1]["max_usd"]))


def _map_summary(tender_map: TenderMap | None) -> str:
    if tender_map is None:
        return "The map step failed, so there is no tender map. Work from the corpus directly."
    summary = tender_map.model_dump(mode="json", exclude={"lenses", "citations"})
    return defuse(canonical(summary).decode())


def lens_message(lens: Lens, tender_map: TenderMap | None, caps: LensCaps) -> str:
    focus: list[str] = []
    if tender_map is not None:
        for plan in tender_map.lenses:
            if plan.lens_id == lens.id:
                focus = plan.focus_questions
    questions = "\n".join(f"- {q}" for q in focus) if focus else "- None from the map; follow the checklist."
    tools = ", ".join(sorted(lens.tools))
    return f"""\
<task>
You are the review lens `{lens.id}`. Your instructions:

{lens.body.strip()}

Tools open to this lens: {tools}.
You have at most {caps.max_turns} turns. Search and read what you need, then call submit_flags once with every \
material finding (an empty list if there is none). Code checks every citation and quote before it accepts a finding.
</task>
<tender_map>
The map step's summary of the tender. It was written by a model from the same untrusted documents: use it to find \
your way, and check anything you rely on against the blocks.
{_map_summary(tender_map)}
</tender_map>
<focus_questions>
{questions}
</focus_questions>"""


@dataclass
class GranskningResult:
    run_id: str
    map: MapResult
    lenses: list[LensResult] = field(default_factory=list)
    prefix_tokens: int = 0
    prefix_measured: bool = False
    caps: LensCaps | None = None
    summary_blob: str | None = None

    @property
    def cost_usd(self) -> float:
        return self.map.outcome.cost_usd + sum(r.cost_usd for r in self.lenses)


class _Events:
    def __init__(self, client: ModelClient) -> None:
        self.client = client

    def __call__(self, step: str, kind: str, **fields: Any) -> None:
        store = self.client.store
        if store is None:
            return
        event = Event(
            run_id=self.client.run_id,
            company_id=self.client.company_id,
            step=step,
            kind=kind,
            at=datetime.now(UTC),
            **fields,
        )
        try:
            store.append_event(event)
        except OSError:
            self.client.persist_failures += 1

    def blob(self, value: Any) -> str | None:
        store = self.client.store
        if store is None:
            return None
        try:
            return store.put_blob(canonical(value))
        except OSError:
            self.client.persist_failures += 1
            return None


def run_granskning(
    client: ModelClient,
    corpus: Corpus,
    calc: list[CalcPost],
    lenses: Sequence[Lens],
    *,
    toolbox: Toolbox | None = None,
    validator: Validator = pydantic_validator,
    concurrency: int = DEFAULT_CONCURRENCY,
    effort: str = EFFORT,
    on_progress: Callable[[str], None] | None = None,
    summary_file: str = "granskning.json",
    code: dict[str, Any] | None = None,
    prefix_plan: dict[str, Any] | None = None,
) -> GranskningResult:
    """Map, then every lens in parallel. Returns when every lens has finished, failed or hit its cap."""
    events = _Events(client)
    say = on_progress or (lambda _msg: None)
    toolbox = toolbox or Toolbox(corpus, calc)
    system = system_blocks(corpus, calc)
    started = time.monotonic()
    events("granskning", "start", size_bytes=sum(len(b["text"].encode()) for b in system))

    say("map: running")
    mapped = run_map(client, system=system, corpus=corpus, toolbox=toolbox, lenses=lenses, effort=effort)
    map_blob = events.blob(
        {
            "status": mapped.outcome.status,
            "error": mapped.outcome.error,
            "tender_map": mapped.tender_map.model_dump(mode="json") if mapped.tender_map else None,
            "problems": mapped.problems,
            "tool_log": [c.model_dump(mode="json") for c in mapped.outcome.tool_log],
        }
    )
    events("map", "result" if mapped.tender_map else "error", cost_usd=mapped.outcome.cost_usd, blob=map_blob)
    map_error = f" ({mapped.outcome.error})" if mapped.outcome.error else ""
    say(f"map: {mapped.outcome.status}{map_error}, {'valid' if mapped.tender_map else 'no valid map'}")
    if mapped.tender_map is not None:
        toolbox.set_roles({d.doc_id: d.role for d in mapped.tender_map.documents})

    measured = mapped.prefix_tokens
    estimated = estimate_tokens(system)
    prefix_tokens = measured or estimated
    caps = lens_caps(client.config.budgets, prefix_tokens)
    result = GranskningResult(
        client.run_id, mapped, prefix_tokens=prefix_tokens, prefix_measured=measured is not None, caps=caps
    )
    lens_blobs: dict[str, str | None] = {}
    lens_errors: dict[str, str | None] = {}

    def one(lens: Lens) -> LensResult:
        say(f"lens {lens.id}: running")
        try:
            lens_result = run_lens(
                client,
                lens_id=lens.id,
                role=lens.role,
                system=system,
                first_message=lens_message(lens, mapped.tender_map, caps),
                toolbox=toolbox,
                max_turns=caps.max_turns,
                max_usd=caps.max_usd,
                validator=validator,
                allowed=lens.tools,
                generic=lens.generic,
                max_tokens=LENS_MAX_TOKENS,
                effort=effort,
                on_outcome=lambda o: lens_errors.__setitem__(lens.id, o.error),
            )
        except Exception as exc:  # noqa: BLE001 - one lens's bug must not lose the others' paid work
            lens_errors[lens.id] = type(exc).__name__
            lens_result = LensResult(
                lens_id=lens.id, model=client.config.model_for(lens.role), status="error", error=type(exc).__name__
            )
        blob = events.blob(lens_result.model_dump(mode="json"))
        lens_blobs[lens.id] = blob
        kind = "error" if lens_result.status == "error" else "result"
        events(f"lens:{lens.id}", kind, cost_usd=lens_result.cost_usd, blob=blob)
        error = f" ({lens_errors[lens.id]})" if lens_errors.get(lens.id) else ""
        say(f"lens {lens.id}: {lens_result.status}{error}, {len(lens_result.flags)} flags, {lens_result.turns} turns")
        return lens_result

    if mapped.outcome.status == "error" and mapped.outcome.error in FATAL_ERRORS:
        say(f"stopping before the lenses: {mapped.outcome.error}")
    else:
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            result.lenses = list(pool.map(one, lenses))

    summary = {
        "run_id": client.run_id,
        "prefix_tokens": prefix_tokens,
        "prefix": {
            "chars": prefix_chars(system),
            "estimated_tokens": estimated,
            "estimate_chars_per_token": CHARS_PER_TOKEN,
            "measured_tokens": measured,  # cache write plus cache read on the map's first call; None if it failed
            "tier_from": "measured" if measured is not None else "estimate",
        },
        "code": code,
        "selection": excluded_counts(corpus),
        "prefix_plan": prefix_plan,
        "caps": {"max_turns": caps.max_turns, "max_usd": caps.max_usd},
        "map": {
            "status": mapped.outcome.status,
            "error": mapped.outcome.error,
            "valid": mapped.tender_map is not None,
            "turns": mapped.outcome.turns,
            "cost_usd": mapped.outcome.cost_usd,
            "blob": map_blob,
        },
        "lenses": [
            {
                "lens_id": r.lens_id,
                "status": r.status,
                "flags": len(r.flags),
                "rejected": len(r.rejected),
                "turns": r.turns,
                "cost_usd": r.cost_usd,
                "blob": lens_blobs.get(r.lens_id),
                "error": lens_errors.get(r.lens_id),
            }
            for r in result.lenses
        ],
        "cost_usd": result.cost_usd,
    }
    result.summary_blob = events.blob(summary)
    store = client.store
    if store is not None:
        try:
            store.write_file(store.run_dir(client.run_id) / summary_file, canonical(summary))
        except OSError:
            client.persist_failures += 1
    duration = int((time.monotonic() - started) * 1000)
    events("granskning", "end", duration_ms=duration, cost_usd=result.cost_usd, blob=result.summary_blob)
    return result
