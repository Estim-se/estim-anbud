"""Categories of calc posts that a plant removes or cuts as a whole, the way an estimator misses them.

Estim's own word lists. A post belongs to a category when its text matches every pattern in `all_of` and none in
`none_of`. A calc whose wording fits no list still gets plants: each of its sections is a category too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from anbud.granskning.contracts import CalcPost


@dataclass(frozen=True)
class Category:
    key: str
    all_of: tuple[str, ...]
    none_of: tuple[str, ...] = ()

    def matches(self, text: str) -> bool:
        return all(re.search(p, text, re.IGNORECASE) for p in self.all_of) and not any(
            re.search(p, text, re.IGNORECASE) for p in self.none_of
        )


CATEGORIES: tuple[Category, ...] = (
    Category(
        "switches",
        (r"\b(ström|kron|växel|kors|tryck)?brytare|strömställare|dimmer|trappautomat",),
        (r"jordfel|dvärg|huvudbrytare|effektbrytare|lastbrytare|säkerhetsbrytare|motorskydd",),
    ),
    Category("outlets", (r"\b(väggu|elu|u)ttag",), (r"data|tele|antenn|tv-",)),
    Category(
        "common_area_demolition",
        (r"rivning|demontering|\briv", r"trapphus|källare|allmän|vind|gemensam|förråd|tvättstuga|korridor"),
    ),
    Category("inspection_hours", (r"besiktning|egenkontroll|provning|kontrollmätning|protokoll",)),
    Category("project_management", (r"projektledning|arbetsledning|byggmöte|samordning",)),
    Category("temporary_power", (r"byggström|byggcentral|provisoris|tillfällig (el|ström|belysning)",)),
    Category("documentation", (r"relationshandling|dokumentation|drift- och underhåll|ritningsrevidering",)),
    Category("lighting", (r"armatur|belysning|downlight|plafond|ljuspunkt",), (r"tillfällig|byggbelysning",)),
    Category("cabling", (r"kabel|ledning|förläggning|kabelstege|kanalisation",)),
    Category("distribution_boards", (r"(grupp|el|under)central|mätarskåp|fördelningscentral",), (r"bygg",)),
    Category("fire_alarm", (r"brandlarm|rökdetektor|brandvarnare|utrymningslarm",)),
    Category("ev_charging", (r"laddbox|laddpunkt|laddstation|elbil",)),
)


def section_category(section: str) -> Category:
    return Category("section:" + section, (re.escape(section),))


def members(category: Category, posts: list[CalcPost]) -> list[int]:
    """Indices of the posts in the category. A section category takes the whole section, notes included."""
    if category.key.startswith("section:"):
        title = category.key.removeprefix("section:")
        return [i for i, p in enumerate(posts) if p.section == title]
    return [i for i, p in enumerate(posts) if category.matches(p.text)]
