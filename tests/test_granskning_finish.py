"""Everything after the lenses, end to end on the synthetic tender with a fake verifier loop. $0."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from _granskning_synth import CALC_POSTS, basis, catalog, corpus, flag, lens_result, search

from anbud.granskning.catalog import load_named_catalog
from anbud.granskning.export_catalog import export, parse_catalog_source
from anbud.granskning.finish import finish
from anbud.granskning.validate import validate_flags
from anbud.models import Verdict

SYSTEM = [{"type": "text", "text": "Synthetic shared system block."}]


def test_finish_runs_from_lens_results_to_reports(data_root: Path) -> None:
    raw = [
        flag("contract", "Byggström saknas i kalkylen.", money_basis=basis(item="Byggström per etapp", quantity=3)),
        flag(
            "quantities",
            "Kalkylen räknar 24 armaturer men tar inte upp elinstallationen i trapphusen.",
            [("aaa111-000002", "all elinstallation i trapphusen, totalt 24 armaturer")],
            claim_type="quantity",
            money_basis=basis(kind="quantity_gap", post_ref="calc-2", quantity=40),
        ),
    ]
    log = [search("byggström")]
    valid, rejects = validate_flags([f.model_dump(mode="json") for f in raw], log, corpus())
    assert len(valid) == 2 and not rejects
    results = [lens_result("contract", valid[:1], tool_log=log), lens_result("quantities", valid[1:])]
    calls: list[str] = []

    def loop(*, step: str, system_blocks: list[dict], user_message: str, validate: Any) -> Verdict | None:
        assert system_blocks is SYSTEM
        calls.append(step)
        fp = user_message.split('fingerprint "')[1].split('"')[0]
        refute = "Byggström" in user_message.split("<finding>")[1].split("</finding>")[0]
        verdict, reasons = validate(
            {
                "fingerprint": fp,
                "verdict": "refuted" if refute else "confirmed",
                "electrical_scope": True,
                "covered_by_calc": False,
                "would_be_wrong_if": "En annan handling ändrar omfattningen.",
            }
        )
        assert not reasons
        return verdict

    out = data_root / "co-001" / "dev" / "t-0001" / "reports" / "run-0001"
    done = finish(
        run_id="run-0001",
        company_id="co-001",
        tender_id="t-0001",
        results=results,
        corpus=corpus(),
        calc_posts=CALC_POSTS,
        system_blocks=SYSTEM,
        verifier_loop=loop,
        out_dir=out,
        seed="salt",
        catalog=catalog(),
    )
    assert len(calls) == 2
    assert [i.flag.lens_id for i in done.ranked.shown] == ["quantities"]
    assert [i.placement for i in done.ranked.logged] == ["refuted"]
    assert done.paths["html"].exists() and done.paths["xlsx"].exists()


SYNTHETIC_TS = """
export const A: PostType[] = ([
  [1, 'Syntetiskt uttag', 0.5, 100, 4], [2, 'Syntetisk armatur', 1, 900, 6],
] as const).map(x => x);
export const B: PostType[] = ([
  [3, 'Syntetisk kanal (m)', 0.2, 50, 7],
] as const).map(x => x);
export const TENDER_SECTIONS: Record<number, string> = {
  4: 'Rum', 6: 'Allmänt',
};
export const UPDATES = [
  { id: id(2), from: { hours: 1, list_price: 900 }, to: { hours: 1.5, list_price: 1200 } },
];
"""


def test_the_catalog_exporter_reads_the_app_shape(data_root: Path, tmp_path: Path) -> None:
    items = parse_catalog_source(SYNTHETIC_TS)
    assert [i.name for i in items] == ["Syntetiskt uttag", "Syntetisk armatur", "Syntetisk kanal (m)"]
    assert items[1].hours == 1.5 and items[1].unit_price == 1200 and items[2].unit == "m"
    assert items[0].category == "Rum" and items[2].category == "7"
    source = tmp_path / "catalog.ts"
    source.write_text(SYNTHETIC_TS, encoding="utf-8")
    export(source, data_root / "catalogs" / "estim-default.json", hourly_rate=500)
    loaded = load_named_catalog("estim-default")
    assert loaded.default and loaded.label == "Estims standardkatalog (ej kundens)" and loaded.hourly_rate == 500
