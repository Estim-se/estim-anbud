"""`anbud regress <company> <tender> <run_id>`: are the findings a person confirmed on an earlier run still found?

The regression set is `<tender>/labels/regression.json` in the data root. It holds tender content (paraphrases, block
ids), so it never goes into the repo. Each entry is one confirmed catch from a source run: its claim type, the blocks it
cites, the calc posts it names, and a one-line paraphrase.

An entry is **caught** by an item of the run under test when they share a cited block and their claim types are the
same or siblings (the same item in `taxonomy.CLAIM_TYPES`). Two counts are reported:
- **surfaced** (the gate): caught by an item a reader is shown as a priced flag (`shown`) or as a requirement without
  a price (`shown_unpriced`, once that placement exists). Generic findings and logged ones don't count. A catch that
  is found but not surfaced is a miss: the reader never sees it.
- **found:** caught by any item the verifier didn't refute, wherever ranking placed it. Reported next to the gate, it
  tells a catch ranking buried from one the lenses lost.

Output is counts and entry indexes only. The full result goes to `regress.json` in the run folder, or, when the run
folder is read-only (a frozen run), to `<tender>/labels/regress-<run_id>.json`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, get_args

from pydantic import BaseModel, ConfigDict, Field

from anbud.granskning.items import Item, Placement
from anbud.granskning.taxonomy import CLAIM_TYPES
from anbud.models import Flag
from anbud.store import RunStore, canonical

# `shown_unpriced` counts as soon as `Placement` has it; until then only `shown` does.
SURFACED = frozenset({"shown"} | ({"shown_unpriced"} & set(get_args(Placement))))
NOT_FOUND = frozenset({"refuted"})


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RegressionEntry(_Model):
    source_run: str
    flag_number: int = Field(description="The item's index in the source run's items.json, counted from 0")
    fingerprint: str
    claim_type: str
    block_ids: list[str] = Field(min_length=1)
    calc_post_ids: list[str] = Field(default_factory=list)
    paraphrase: str


class RegressionSet(_Model):
    company_id: str
    tender_id: str
    numbering: str = "flag_number is the 0-based index into the source run's items.json (report order)"
    entries: list[RegressionEntry]

    @classmethod
    def load(cls, path: Path) -> RegressionSet:
        return cls.model_validate_json(path.read_bytes())


def entry_from_item(item: Item, source_run: str, flag_number: int, paraphrase: str) -> RegressionEntry:
    posts = [p for p in (item.flag.money_basis.post_ref, item.verdict.calc_post if item.verdict else None) if p]
    return RegressionEntry(
        source_run=source_run,
        flag_number=flag_number,
        fingerprint=item.fingerprint,
        claim_type=item.flag.claim_type,
        block_ids=sorted({c.block_id for c in item.flag.citations}),
        calc_post_ids=sorted(set(posts)),
        paraphrase=paraphrase,
    )


class RunItem(BaseModel):
    """The part of an `Item` regress reads. Other fields are ignored, so runs written by older code still load."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    flag: Flag
    fingerprint: str
    placement: str | None = None


def load_items(run_dir: Path) -> list[RunItem]:
    return [RunItem.model_validate(i) for i in json.loads((run_dir / "items.json").read_text())]


def siblings(a: str, b: str) -> bool:
    """The same claim type, or two claim types under the same taxonomy item."""
    if a == b:
        return True
    return a in CLAIM_TYPES and b in CLAIM_TYPES and CLAIM_TYPES[a][0] == CLAIM_TYPES[b][0]


def matches(entry: RegressionEntry, item: Item | RunItem) -> bool:
    blocks = {c.block_id for c in item.flag.citations}
    return bool(blocks & set(entry.block_ids)) and siblings(entry.claim_type, item.flag.claim_type)


def check(regression: RegressionSet, items: list[Item] | list[RunItem], run_id: str) -> dict[str, Any]:
    results = []
    for index, entry in enumerate(regression.entries):
        hits = [(n, item) for n, item in enumerate(items) if matches(entry, item)]
        found = [(n, i) for n, i in hits if i.placement not in NOT_FOUND]
        surfaced = [(n, i) for n, i in found if i.placement in SURFACED]
        results.append(
            {
                "entry": index,
                "surfaced": bool(surfaced),
                "found": bool(found),
                "items": [{"index": n, "fingerprint": i.fingerprint, "placement": i.placement} for n, i in hits],
            }
        )
    return {
        "run_id": run_id,
        "entries": len(results),
        "surfaced": sum(r["surfaced"] for r in results),
        "found": sum(r["found"] for r in results),
        "passed": all(r["surfaced"] for r in results),
        "not_surfaced": [r["entry"] for r in results if not r["surfaced"]],
        "not_found": [r["entry"] for r in results if not r["found"]],
        "surfaced_placements": sorted(SURFACED),
        "results": results,
    }


def regress(company_id: str, tender_dir: Path, run_id: str, root: Path | None = None) -> tuple[dict[str, Any], Path]:
    """The result, and where it was written."""
    run_dir = tender_dir / "runs" / run_id
    if not (run_dir / "items.json").exists():
        raise FileNotFoundError("the run has no items.json")
    regression = RegressionSet.load(tender_dir / "labels" / "regression.json")
    if regression.company_id != company_id or regression.tender_id != tender_dir.name:
        raise ValueError("the regression set belongs to another tender")
    result = check(regression, load_items(run_dir), run_id)
    store = RunStore(company_id, root)
    out = run_dir / "regress.json" if os.access(run_dir, os.W_OK) else tender_dir / "labels" / f"regress-{run_id}.json"
    store.write_file(out, canonical(result))
    return result, out
