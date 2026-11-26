"""Everything after the lenses, in order: merge, provisional kronor, verifier selection and loop, final kronor, rank and
cap, coverage, reports. The tool loop and the shared system blocks come from the caller. With `out_dir` None the
report is built but not written, so the caller can add what it knows only afterwards (the run's cost).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anbud.granskning.catalog import Catalog
from anbud.granskning.checks import ChecksReport
from anbud.granskning.contracts import CalcPost, LensResult
from anbud.granskning.corpus import Corpus
from anbud.granskning.items import Item
from anbud.granskning.kronor import ranking_kronor
from anbud.granskning.merge import merge
from anbud.granskning.rank import Ranked, coverage, price_items, rank
from anbud.granskning.report import Report, build_report, write_reports
from anbud.granskning.settings import Settings, load_settings
from anbud.granskning.verify import VerifierLoop, run_verifier, select
from anbud.model_client import ModelClient


@dataclass
class Finished:
    items: list[Item]
    ranked: Ranked
    report: Report
    paths: dict[str, Path]


def finish(
    *,
    run_id: str,
    company_id: str,
    tender_id: str,
    results: list[LensResult],
    corpus: Corpus,
    calc_posts: list[CalcPost],
    system_blocks: list[dict[str, Any]],
    verifier_loop: VerifierLoop,
    out_dir: Path | None,
    seed: str,
    catalog: Catalog | None,
    merge_client: ModelClient | None = None,
    settings: Settings | None = None,
    named_not_present: list[str] | None = None,
    unread_tiles: list[str] | None = None,
    action_rates: dict[str, float] | None = None,
    parallel: int = 1,
    cost_usd: float | None = None,
    check_results: Sequence[LensResult] = (),
    checks: ChecksReport | None = None,
) -> Finished:
    """`check_results` are the focused checks' flags: merged, verified and ranked with the lenses' flags, but left out
    of the lens coverage. `checks` is the checks' report sections and coverage."""
    settings = settings or load_settings()
    posts = {p.id: p for p in calc_posts}
    items = merge([*results, *check_results], corpus, merge_client, settings)
    provisional = price_items(items, catalog, posts, settings)
    chosen = select(
        provisional,
        lambda i: ranking_kronor(i.kronor, settings),
        settings.verifier_top_n,
        settings.verifier_sampled,
        seed,
    )
    verified = {
        i.fingerprint: i for i in run_verifier(chosen, corpus, system_blocks, verifier_loop, set(posts), parallel)
    }
    final = price_items([verified.get(i.fingerprint, i) for i in provisional], catalog, posts, settings)
    ranked = rank(final, settings, action_rates)
    report = build_report(
        run_id=run_id,
        company_id=company_id,
        tender_id=tender_id,
        ranked=ranked,
        coverage=coverage(results, corpus, named_not_present, unread_tiles),
        corpus=corpus,
        catalog=catalog,
        materiality_sek=settings.materiality_sek,
        max_shown=settings.max_shown,
        cost_usd=cost_usd,
    )
    if checks is not None:
        report = report.model_copy(update={"checks": checks})
    paths = write_reports(report, out_dir) if out_dir is not None else {}
    return Finished(final, ranked, report, paths)
