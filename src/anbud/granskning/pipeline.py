"""One Granskning run, end to end: page cards, map and lenses, checks, merge, verifier, kronor, rank, reports.

1. R1 page cards on every tile; their tile_text blocks join the corpus before the shared prefix is built. A security
   marking in a card stops the run like a marking found by prep.
2. Map and lenses (`run_granskning`), with `validate_flags` as the validator and the taxonomy's claim types enforced.
3. Merge, the verifier on the shared tool loop, kronor, rank and cap, coverage, and the HTML and XLSX reports.
4. Next to the report: `inputs.json` (RunInputs, with the calc posts), `flags.json` (the merged flags, in report
   order), `kronor.json` (Kronor by fingerprint) and `items.json` (each finding with its verdict and placement).

Every model call goes through the one ModelClient, so the step cache makes a rerun or `--replay` cost $0.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anbud.granskning.catalog import Catalog, with_calc_rate
from anbud.granskning.contracts import CalcPost, RunInputs
from anbud.granskning.corpus import Corpus
from anbud.granskning.finish import Finished, finish
from anbud.granskning.lenses import Lens
from anbud.granskning.loop import LoopOutcome, SubmitOutcome, run_tool_loop
from anbud.granskning.merge import STOP_ERRORS
from anbud.granskning.page_cards import Cards, read_cards
from anbud.granskning.prefix import system_blocks
from anbud.granskning.report import write_reports
from anbud.granskning.run import FATAL_ERRORS, GranskningResult, run_granskning
from anbud.granskning.settings import Settings, load_settings
from anbud.granskning.taxonomy import CLAIM_TYPES
from anbud.granskning.toolbox import Toolbox
from anbud.granskning.validate import flag_validator
from anbud.granskning.verify import VerdictValidator, VerifierLoop
from anbud.model_client import ModelClient, ModelError
from anbud.models import Verdict
from anbud.prep.tender import IntakeStop
from anbud.store import RunStore, StoreError, canonical


def add_cards(corpus: Corpus, cards: Cards) -> Corpus:
    """A new corpus with the cards' tile_text blocks after each document's own blocks. The corpus passed in is left
    as it was."""
    new = [b for b in cards.blocks if b.id not in corpus.blocks]
    blocks = dict(corpus.blocks) | {b.id: b for b in new}
    return Corpus(blocks, [*corpus.order, *(b.id for b in new)], corpus.outline, corpus.tiles, corpus.documents)


@dataclass
class CardsRead:
    cards: Cards
    failed: dict[str, str] = field(default_factory=dict)  # tile id -> error kind


def read_all_cards(client: ModelClient, store: RunStore, corpus: Corpus) -> CardsRead:
    """One card per tile, tile by tile, so one failure loses one tile. A stop, the budget, the spend cap or a replay
    miss ends the cards there; the run goes on without the rest, and coverage says which tiles weren't read."""
    out = CardsRead(Cards())
    tiles = sorted(corpus.tiles.values(), key=lambda t: (t.doc_id, t.page, t.index, t.id))
    for n, tile in enumerate(tiles):
        try:
            one = read_cards(client, store, [tile])
        except (*STOP_ERRORS, OSError, StoreError) as exc:
            kind = exc.kind if isinstance(exc, ModelError) else type(exc).__name__
            out.failed[tile.id] = kind
            if kind in FATAL_ERRORS:
                out.failed.update({t.id: kind for t in tiles[n + 1 :]})
                break
            continue
        out.cards.blocks += one.blocks
        out.cards.masked += one.masked
        out.cards.markings += one.markings
        out.cards.cost_usd += one.cost_usd
    if out.cards.markings:
        raise IntakeStop(out.cards.markings)
    return out


@dataclass
class VerifierRuns:
    """The verifier's tool loops, for the run summary: status and cost per flag."""

    outcomes: dict[str, LoopOutcome] = field(default_factory=dict)

    @property
    def cost_usd(self) -> float:
        return sum(o.cost_usd for o in self.outcomes.values())


def verifier_loop(
    client: ModelClient, toolbox: Toolbox, settings: Settings, runs: VerifierRuns | None = None
) -> VerifierLoop:
    """`VerifierLoop` on the shared tool loop: role `verifier`, `submit_verdict`, one repair turn, the verifier caps."""

    def loop(
        *, step: str, system_blocks: list[dict[str, Any]], user_message: str, validate: VerdictValidator
    ) -> Verdict | None:
        accepted: list[Verdict] = []

        def on_submit(raw: Any, tool_log: Any, final: bool) -> SubmitOutcome:
            verdict, reasons = validate(raw if isinstance(raw, dict) else {})
            if verdict is not None:
                accepted.append(verdict)
                return SubmitOutcome(True, "Verdict received.")
            if final:
                return SubmitOutcome(True, "Verdict received but it failed the checks; it is not used.")
            lines = "\n".join(f"- {r}" for r in reasons)
            return SubmitOutcome(
                False, f"The verdict failed these checks. Fix them and call submit_verdict again:\n{lines}"
            )

        outcome = run_tool_loop(
            client,
            role="verifier",
            step=step,
            system=system_blocks,
            first_message=user_message,
            toolbox=toolbox,
            submit_tool="submit_verdict",
            on_submit=on_submit,
            max_turns=settings.verifier_max_turns,
            max_usd=settings.verifier_max_usd,
            max_tokens=settings.verifier_max_tokens,
            effort=settings.verifier_effort,
        )
        if runs is not None:
            runs.outcomes[step] = outcome
        return accepted[0] if accepted else None

    return loop


@dataclass
class PipelineResult:
    run_id: str
    cards: Cards
    unread_tiles: dict[str, str]
    granskning: GranskningResult
    finished: Finished
    verifier: VerifierRuns
    catalog: Catalog | None
    run_dir: Path
    files: dict[str, Path]
    cost_usd: float = 0.0  # what this pipeline spent, from the run budget: every call, merge and failures included


def _write(store: RunStore, path: Path, value: Any) -> None:
    store.write_file(path, json.dumps(value, ensure_ascii=False, indent=1).encode() + b"\n")


def run_pipeline(
    client: ModelClient,
    store: RunStore,
    corpus: Corpus,
    calc: list[CalcPost],
    lenses: Sequence[Lens],
    *,
    tender_id: str,
    prep_dir: Path,
    run_dir: Path,
    catalog: Catalog | None,
    settings: Settings | None = None,
    concurrency: int = 3,
    on_progress: Callable[[str], None] | None = None,
    summary_file: str = "granskning.json",
    code: dict[str, Any] | None = None,
) -> PipelineResult:
    settings = settings or load_settings()
    say = on_progress or (lambda _msg: None)
    spent_before = client.budget.spent
    say(f"page cards: {len(corpus.tiles)} tiles")
    read = read_all_cards(client, store, corpus)
    cards = read.cards
    failed = (
        f", {len(read.failed)} tiles failed ({', '.join(sorted(set(read.failed.values())))})" if read.failed else ""
    )
    say(f"page cards: {len(cards.blocks)} blocks{failed}, ${cards.cost_usd:.2f}")
    if cards.masked:  # the originals stay local, like prep's masking report
        _write(store, run_dir / "cards_masking.json", [m.model_dump(mode="json") for m in cards.masked])
    corpus = add_cards(corpus, cards)

    toolbox = Toolbox(corpus, calc, tile_loader=store.get_blob, catalog=catalog)
    validator = flag_validator(corpus, set(CLAIM_TYPES))
    result = run_granskning(
        client,
        corpus,
        calc,
        lenses,
        toolbox=toolbox,
        validator=validator,
        concurrency=concurrency,
        on_progress=say,
        summary_file=summary_file,
        code=code,
    )

    if catalog is not None:
        catalog = with_calc_rate(catalog, calc)
    runs = VerifierRuns()
    named = result.map.tender_map.documents_named_not_present if result.map.tender_map else []
    say(f"verifier: up to {settings.verifier_top_n} + {settings.verifier_sampled} sampled")
    finished = finish(
        run_id=client.run_id,
        company_id=client.company_id,
        tender_id=tender_id,
        results=result.lenses,
        corpus=corpus,
        calc_posts=calc,
        system_blocks=system_blocks(corpus, calc),
        verifier_loop=verifier_loop(client, toolbox, settings, runs),
        out_dir=None,
        seed=client.salt or client.run_id,
        catalog=catalog,
        merge_client=client,
        settings=settings,
        named_not_present=named,
        unread_tiles=sorted(read.failed),
        parallel=concurrency,
    )
    cost = client.budget.spent - spent_before  # every call this pipeline made, merge and failed calls included
    finished.report = finished.report.model_copy(update={"cost_usd": round(cost, 4)})
    finished.paths = write_reports(finished.report, run_dir / "report")
    ranked = finished.ranked
    ordered = ranked.shown + ranked.unpriced + ranked.generic + ranked.logged
    files = dict(finished.paths)
    files["inputs"] = run_dir / "inputs.json"
    inputs = RunInputs(company_id=client.company_id, tender_id=tender_id, prep_dir=str(prep_dir), calc_posts=calc)
    store.write_file(files["inputs"], canonical(inputs.model_dump(mode="json")))
    files["flags"] = run_dir / "flags.json"
    _write(store, files["flags"], [i.flag.model_dump(mode="json") for i in ordered])
    files["kronor"] = run_dir / "kronor.json"
    _write(store, files["kronor"], {i.fingerprint: i.kronor.model_dump(mode="json") for i in ordered if i.kronor})
    files["items"] = run_dir / "items.json"
    _write(store, files["items"], [i.model_dump(mode="json") for i in ordered])
    return PipelineResult(client.run_id, cards, read.failed, result, finished, runs, catalog, run_dir, files, cost)
