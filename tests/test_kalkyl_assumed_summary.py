"""Översikt says how much of the total rests on assumed counts and names the largest assumed posts with the citation
of the line the assumption sits on, so the estimator sees at once that the figure is an assumption and what to check.
Synthetic lines only."""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import openpyxl
import pytest
from kalkyl_synthetic import (
    BLOCKS,
    CATALOG,
    DOC_A,
    GOOD,
    OUTLINE,
    cite,
    fake_sdk,
    first_block,
    good_handler,
    message,
    req,
    submit,
    write_catalog,
    write_prep,
)

from anbud import paths
from anbud.kalkyl.run import run_dev_tender
from anbud.models import Block


def _block(n: int, text: str) -> Block:
    return Block(id=f"{DOC_A[:6]}-{n:06d}", doc_id=DOC_A, kind="paragraph", text=text, page=3)


# Each line names an item per apartment and writes no count: one per apartment is assumed.
ASSUMED = [
    (_block(20, "Laddbox i varje lägenhet."), "Laddbox", "laddbox", 12000, 6),
    (_block(21, "Spisvakt i varje lägenhet."), "Spisvakt", "spisvakt", 1500, 1),
    (_block(22, "Svarsapparat för porttelefon i varje lägenhet."), "Svarsapparat", "svarsapparat", 800, 1),
    (_block(23, "Brandvarnare i varje lägenhet."), "Brandvarnare", "brandvarnare", 300, 0.5),
]


@pytest.fixture
def tender(data_root: Path) -> Path:
    tender = paths.dev_tender_dir("co-001", "t-0001")
    write_prep(tender / "prep", [*BLOCKS[:12], *(a[0] for a in ASSUMED), *BLOCKS[12:]], OUTLINE)
    catalog = copy.deepcopy(CATALOG)
    for _, name, item, price, hours in ASSUMED:
        catalog["items"].append(
            {"id": item, "name": name, "unit": "st", "unit_price": price, "hours": hours, "category": "Övrigt"}
        )
    write_catalog(data_root, catalog=catalog)
    return tender


def _handler(params: dict[str, Any]) -> Any:
    if "tools" in params and params["tools"][0]["name"] == "submit_quantities":
        if first_block(params) == "aaaaaa-000001":
            extra = [
                req(
                    name,
                    "per_object",
                    "st",
                    [cite(block.id, block.text.rstrip("."))],
                    quantity=1,
                    per_object="apartment",
                    catalog_id=item,
                )
                for block, name, item, _, _ in ASSUMED
            ]
            return message([submit({"records": [*GOOD["aaaaaa-000001"]["records"], *extra], "set_aside": []})])
    return good_handler(params)


def test_the_overview_gives_the_assumed_share_and_names_the_largest_assumed_posts(tender: Path) -> None:
    run_dev_tender("co-001", "t-0001", sdk=fake_sdk(_handler), catalog_name="synthetic", run_id="kalkyl-a")
    out = tender / "kalkyl" / "kalkyl-a"
    result = json.loads((out / "result.json").read_text())
    posts = {p["name"]: p for p in result["posts"]}
    total, assumed = Decimal(result["totals"]["total"]), Decimal(result["totals"]["assumed_total"])
    assert assumed == sum(Decimal(posts[a[1]]["total"]) for a in ASSUMED)  # the four, and nothing unpriced
    assert posts["Lägenhetscentral"]["level"] == "Antagande" and posts["Lägenhetscentral"]["status"] == "Saknar pris"

    wb = openpyxl.load_workbook(out / "kalkyl.xlsx")
    ws = wb["Översikt"]
    assert [c.value for c in ws[4]] == ["Uppgift", "Värde", "Underlag", "Källa (block)", "Citat"]
    rows = {r[0].value: r for r in ws.iter_rows(min_row=5) if r[0].value}
    labels = list(rows)
    share = rows["Andel av summan som vilar på antagna mängder"]
    assert share[1].value == pytest.approx(float(assumed / total)) and share[1].number_format == "0%"
    assert share[1].fill.fgColor.rgb.endswith("FCE4D6")  # marked, like an assumed post's level
    # Right under the sum, so it is read with it.
    assert labels.index("Andel av summan som vilar på antagna mängder") == labels.index("Summa (exkl. optioner)") + 2

    # The three largest assumed posts, largest first, each with its share, basis and the cited line.
    top = [label for label in labels if str(label).startswith("Antagen post att kontrollera")]
    names = ("Laddbox", "Spisvakt", "Svarsapparat")
    assert top == [f"Antagen post att kontrollera {n}: {name}" for n, name in enumerate(names, 1)]
    for label, (block, name, *_) in zip(top, ASSUMED, strict=False):
        row = rows[label]
        post = posts[name]
        assert row[1].value == pytest.approx(float(post["total"]))
        pct = (Decimal(post["total"]) / total * 100).quantize(Decimal(1))
        assert row[2].value == f"{pct} % av summan; 1 per lägenhet (antaget) × 3 lägenheter; {post['id']}"
        assert row[3].value == block.id and row[4].value == block.text.rstrip(".")
    rest = rows["Övriga antagna poster (1)"]
    assert rest[1].value == pytest.approx(float(posts["Brandvarnare"]["total"]))
    assert not any("Lägenhetscentral" in str(label) for label in labels)  # assumed but unpriced: not in the sum
