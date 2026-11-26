"""What the focused checks would do on a tender, without a model call: for `anbud run --dry-run`."""

from __future__ import annotations

import math

from anbud.granskning.contracts import CalcPost
from anbud.granskning.corpus import Corpus
from anbud.granskning.outlet_recipe import RecipeError, load_recipe, trigger_hints
from anbud.granskning.zero_rows import BATCH, zero_rows


def checks_line(corpus: Corpus, calc: list[CalcPost], company_id: str | None) -> str:
    zeros = len(zero_rows(calc))
    hints = len(trigger_hints(corpus))
    try:
        recipe = "yes" if company_id and load_recipe(company_id) else "none"
    except RecipeError:
        recipe = "unreadable"
    return (
        f"{zeros} zero or not-included calc rows ({math.ceil(zeros / BATCH)} batches); per-space rules: 1 call; "
        f"outlet trigger candidates {hints} ({'1 call' if hints else 'skipped'}), customer recipe {recipe}"
    )
