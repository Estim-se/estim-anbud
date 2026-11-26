"""Core data models. The submit_* tool inputs are built from these and validated in code (non-strict tools).

MoneyBasis is one flat object with a `kind` enum, never a union, to stay inside strict-mode schema limits.
Event carries ids, sizes, timings and costs only, never document text or model output.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import SkipJsonSchema


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Box(_Model):
    x0: float
    y0: float
    x1: float
    y1: float


class Block(_Model):
    """A citable unit of a tender document: a paragraph, heading, table row or tile text."""

    id: str = Field(description="Stable id: file content hash plus normalised text hash")
    doc_id: str
    kind: Literal["heading", "paragraph", "table_row", "sheet_row", "tile_text"]
    text: str
    page: int | None = None
    line: int | None = None
    sheet: str | None = None
    row: int | None = None
    box: Box | None = None
    hidden: bool = Field(default=False, description="Hidden row, column or sheet in a spreadsheet")


class Citation(_Model):
    block_id: str
    quote: str = Field(description="Exact text from the block; checked by code after normalisation")


class DocumentRole(_Model):
    doc_id: str
    role: str = Field(description="For example AF, rambeskrivning, drawing, schedule, calc")
    revision: str | None = None


class LensPlan(_Model):
    lens_id: str
    focus_questions: list[str] = Field(min_length=3, max_length=6)


class TenderMap(_Model):
    documents: list[DocumentRole]
    contract_form: str | None = None
    building_type: str | None = None
    occupancy: str | None = None
    stated_scope: str
    stage_plan: str | None = None
    documents_named_not_present: list[str] = Field(default_factory=list)
    permit_date: str | None = None
    lenses: list[LensPlan]
    citations: list[Citation] = Field(default_factory=list)


class MoneyBasis(_Model):
    """How code prices a flag. Flat on purpose: one object, `kind` says which fields apply."""

    kind: Literal["missing_post", "quantity_gap", "hours_gap", "contract_formula"]
    post_ref: str | None = Field(default=None, description="Calc post id, if one is affected")
    item: str | None = Field(default=None, description="Catalog item or description for a missing post")
    catalog_id: str | None = Field(
        default=None, description="The catalog item's id from catalog_search, for a missing_post or quantity_gap"
    )
    quantity: float | None = None
    unit: str | None = None
    hours: float | None = None
    formula: Literal["vite_per_week", "security", "index_drift", "wage_step"] | None = None
    formula_inputs: list[FormulaInput] = Field(default_factory=list)


class FormulaInput(_Model):
    name: str
    value: float


class Flag(_Model):
    lens_id: str
    claim_type: str = Field(description="Taxonomy item, for example trade_boundary or contract_vite")
    claim: str
    citations: list[Citation] = Field(min_length=1)
    money_basis: MoneyBasis
    absence: bool = Field(default=False, description="The finding is that something is missing")
    searches: list[str] = Field(default_factory=list, description="Searches run, required for an absence finding")
    reference_rule: str | None = Field(default=None, description="The rule an absence is measured against")
    contractual_exposure: bool = False
    # Set by code for a focused check's flag (one per outlet item kind, space rule or zero-row post), never by a model:
    # it keeps the submit_flags schema unchanged, and lens items lose it before validation.
    check_key: SkipJsonSchema[str | None] = None


class Verdict(_Model):
    fingerprint: str = Field(description="sha256 of the flag's sorted block ids plus claim type")
    verdict: Literal["confirmed", "partly", "refuted"]
    electrical_scope: bool = Field(description="The money belongs to the electrical subcontractor")
    covered_by_calc: bool
    calc_post: str | None = None
    corrected_money_basis: MoneyBasis | None = None
    would_be_wrong_if: str


class Event(_Model):
    """One line of a run's append-only log."""

    run_id: str
    company_id: str
    step: str
    kind: Literal["start", "model_call", "cache_hit", "tool_call", "result", "error", "end"]
    at: datetime
    duration_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    blob: str | None = Field(default=None, description="Content hash of the stored response")
    request_blob: str | None = Field(default=None, description="Content hash of the stored request")
    size_bytes: int | None = None


MoneyBasis.model_rebuild()
