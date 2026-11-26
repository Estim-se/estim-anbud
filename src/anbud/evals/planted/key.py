"""The answer key of a planted variant: every plant, what it changed and where, and the flag it should draw.

The key lives in `key/plants.json` beside the arms, never inside one, so no run reads it. It holds document and calc
text, so it stays in the company's data folder like the files themselves.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from anbud.evals.planted.structural import Finding

BasisKind = Literal["missing_post", "quantity_gap", "hours_gap", "contract_formula"]
Arm = Literal["planted", "unplanted", "calc_only"]
ARMS: tuple[Arm, ...] = ("planted", "unplanted", "calc_only")
PlantKind = Literal[
    "remove_category",
    "cut_quantity",
    "cut_hours",
    "contradicting_quantity",
    "vite_clause",
    "index_clause",
    "delete_section",
    "drawing_note_conflict",
]
CALC_KINDS: tuple[PlantKind, ...] = ("remove_category", "cut_quantity", "cut_hours")
DOC_KINDS: tuple[PlantKind, ...] = (
    "contradicting_quantity",
    "vite_clause",
    "index_clause",
    "delete_section",
    "drawing_note_conflict",
)
ALL_BASIS: list[BasisKind] = ["missing_post", "quantity_gap", "hours_gap", "contract_formula"]

# The claim types each kind of plant should draw, from `granskning.taxonomy.CLAIM_TYPES` (a test keeps them in it).
# The matcher reads only these two tables.
EXPECTED_CLAIMS: dict[PlantKind, list[str]] = {
    "remove_category": ["unpriced_requirement", "generic_scope"],
    "cut_quantity": ["quantity_realism", "unpriced_requirement"],
    "cut_hours": ["hours_realism"],
    "contradicting_quantity": ["document_conflict", "quantity_realism", "unreliable_information"],
    "vite_clause": ["contract_vite"],
    "index_clause": ["contract_index"],
    "delete_section": ["missing_document", "document_conflict"],
    "drawing_note_conflict": ["document_conflict", "quantity_realism"],
}

# Calc categories whose taxonomy item has a claim type of its own: a flag may name that item instead.
CATEGORY_CLAIMS: dict[str, list[str]] = {
    "inspection_hours": ["documentation_inspection"],
    "documentation": ["documentation_inspection", "design_responsibility"],
    "project_management": ["generic_scope", "occupied_building"],
    "temporary_power": ["occupied_building"],
    "fire_alarm": ["regulation_fire_safety"],
    "ev_charging": ["regulation_ev_charging"],
    "distribution_boards": ["metering", "grid_interface"],
}


def expected_claims(kind: PlantKind, category: str | None = None) -> list[str]:
    return list(dict.fromkeys([*EXPECTED_CLAIMS[kind], *CATEGORY_CLAIMS.get(category or "", [])]))


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PostAt(_Model):
    """A post's place in one arm's regenerated calc."""

    sheet: str
    row: int
    section: str
    text: str


class Location(_Model):
    """Where a flag must point to count for a plant, in one arm."""

    posts: list[PostAt] = Field(default_factory=list)
    sections: list[str] = Field(default_factory=list)
    blocks: list[str] = Field(default_factory=list)
    anywhere: bool = Field(default=False, description="Any flag of the right kind counts, wherever it points")


class Change(_Model):
    target: Literal["calc_post", "block"]
    action: Literal["removed", "mention_removed", "quantity_cut", "hours_cut", "inserted", "replaced", "deleted"]
    post_id: str | None = Field(default=None, description="The source calc post's id")
    at: PostAt | None = Field(default=None, description="The post's place in the planted calc, if it is still there")
    doc_id: str | None = None
    block_id: str | None = Field(default=None, description="The block before the change (or the anchor, inserted)")
    new_block_id: str | None = None
    before: float | None = None
    after: float | None = None
    before_text: str | None = None
    after_text: str | None = None


class Expected(_Model):
    claim_types: list[str]
    basis_kinds: list[BasisKind]
    formula: str | None = None
    quantity: float | None = Field(default=None, description="Quantity the calc or document is short by, in total")
    hours: float | None = Field(default=None, description="Hours the calc is short by, in total")
    kronor: float | None = Field(default=None, description="From the calc's own prices; None when it has none")


class Plant(_Model):
    id: str
    kind: PlantKind
    family: Literal["calc", "document"]
    category: str | None = None
    changes: list[Change]
    expected: Expected
    locations: dict[Arm, Location]


class Key(_Model):
    version: int = 1
    company_id: str
    tender_id: str
    seed: int
    source: dict[str, str] = Field(description="Content hashes of the calc posts and the prep blocks planted on")
    plants: list[Plant]
    structural: list[Finding] = Field(default_factory=list, description="Control arm 1 on the planted calc")
    structural_unplanted: list[Finding] = Field(
        default_factory=list, description="The same detector on the unplanted copy: its noise floor"
    )
