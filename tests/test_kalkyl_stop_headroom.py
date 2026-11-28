"""Kalkyl's calls in flight fit under its hard stop (audit B5, checked). A Kalkyl run never degrades: a call the hard
stop refuses halts it, unfrozen. Each call reserves the most it can cost, up to `concurrency` at once, and the run's
tier is picked from its dry run's high estimate times `margin`. So at the top of a tier, a run that has already spent
its whole high estimate has `stop x (1 - 1/margin)` left, and that must hold a full set of reading calls in flight, or
a run within its estimate halts below its stop. `concurrency` is the most in flight: page cards are read one at a time
before the readings, and the group pass starts after them. A reading call here has a 20,000-token input (the prefix
and the largest chunk message; the dev tender's is just under) and the shipped max_tokens. At the top of S, four such
calls fit while each input is under about 29,500 tokens; M and L leave more. Config only, $0.

Each test names the one-line mutation it catches; each was made, seen to fail the test, and reverted."""

from __future__ import annotations

import pytest

from anbud.config import load_config
from anbud.kalkyl.settings import EXTRACT, load_kalkyl_config
from anbud.model_client import Budget

INPUT_TOKENS = 20_000


@pytest.mark.parametrize("tier", [t.name for t in load_kalkyl_config().stops])
def test_a_run_at_the_top_of_its_tier_that_spent_its_high_estimate_still_fits_its_calls_in_flight(tier: str) -> None:
    """Mutation: `margin = 1.5` -> `margin = 1.3` in config/budgets.toml [kalkyl]: at the top of S, a run that has spent
    its high estimate has 0.69 USD left for 0.84 USD of reservations, and its fourth call in flight halts it."""
    kalkyl, config = load_kalkyl_config(), load_config()
    stop = next(t for t in kalkyl.stops if t.name == tier)
    high_estimate = stop.bound / kalkyl.margin  # the largest high estimate this tier is picked for
    price = config.price_for(config.model_for(EXTRACT))
    call = price.max_cost(INPUT_TOKENS, kalkyl.roles[EXTRACT].max_tokens)
    budget = Budget(stop.usd)
    budget.settle(0.0, high_estimate)  # spent the whole high estimate, nothing in flight
    for _ in range(kalkyl.concurrency):
        budget.reserve(call)  # raises BudgetExceeded if it doesn't fit
    assert budget.spent + budget.reserved <= stop.usd
