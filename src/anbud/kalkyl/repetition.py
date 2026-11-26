"""Repetition: per-unit hours fall when one item is installed many times on one site.

A table gives a factor on the hours per tier of units within one post, as `[[from_unit, factor], ...]` from unit 1
upwards. It is applied like tax brackets: units up to the next tier's start take the first factor, the next units the
next factor, and so on. There is no cliff, and the first units carry their full setup time. Only count units are
affected (a length or an area isn't repetition of one task), and material never changes.

Sources, in order:
1. The customer's norm table, `~/estim-anbud-data/<company>/references/repetition.toml`, labelled "kundens
   normtabell".
2. Estim's default in `config/kalkyl.toml` `[repetition]`, labelled as Estim's assumption. Its values are placeholders
   chosen without calibration data (no tender's figures) for the company to replace with its own norm table.

With neither, hours are flat per unit, as before.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from anbud import paths
from anbud.config import CONFIG_DIR

CUSTOMER_FILE = "references/repetition.toml"
CUSTOMER_LABEL = "kundens normtabell"


class RepetitionError(ValueError):
    pass


@dataclass(frozen=True)
class Repetition:
    label: str
    tiers: tuple[tuple[int, Decimal], ...]  # (first unit of the tier, factor on hours), the first from unit 1

    def hours_factor(self, quantity: Decimal) -> Decimal:
        """The average factor on hours over `quantity` units, tier by tier."""
        if quantity <= 0:
            return Decimal(1)
        total = Decimal(0)
        for i, (start, factor) in enumerate(self.tiers):
            low = Decimal(start - 1)
            high = quantity if i + 1 == len(self.tiers) else min(quantity, Decimal(self.tiers[i + 1][0] - 1))
            if high > low:
                total += (high - low) * factor
        return total / quantity


def parse(raw: dict, label: str) -> Repetition:
    tiers = raw.get("tiers")
    if not isinstance(tiers, list) or not tiers:
        raise RepetitionError("the repetition table needs tiers = [[from_unit, factor], ...]")
    out: list[tuple[int, Decimal]] = []
    for tier in tiers:
        if not (isinstance(tier, list) and len(tier) == 2 and isinstance(tier[0], int)):
            raise RepetitionError("each tier is [from_unit, factor]")
        out.append((tier[0], Decimal(str(tier[1]))))
    starts = [s for s, _ in out]
    factors = [f for _, f in out]
    if starts[0] != 1 or starts != sorted(set(starts)):
        raise RepetitionError("tiers start at unit 1 and rise")
    if any(not (Decimal(0) < f <= 1) for f in factors) or factors != sorted(factors, reverse=True):
        raise RepetitionError("factors lie in (0, 1] and never rise")
    return Repetition(label, tuple(out))


def customer_path(company_id: str, root: Path | None = None) -> Path:
    return paths.company_dir(company_id, root) / CUSTOMER_FILE


def load_repetition(company_id: str, root: Path | None = None, config_dir: Path = CONFIG_DIR) -> Repetition | None:
    path = customer_path(company_id, root)
    try:
        if path.is_file():
            return parse(tomllib.loads(path.read_text(encoding="utf-8")), CUSTOMER_LABEL)
        raw = tomllib.loads((config_dir / "kalkyl.toml").read_text()).get("repetition")
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise RepetitionError(f"the repetition table can't be read ({type(exc).__name__})") from None
    if raw is None:
        return None
    return parse(raw, str(raw.get("label", "Estims standardantagande")))
