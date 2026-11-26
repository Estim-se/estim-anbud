"""What R2 submits, and what code makes of it.

The model reads and cites; it never counts or does arithmetic. A record says what one to three consecutive blocks
state: an object of the building (an apartment row in a room list, a stated total of premises), or a requirement (what
is installed or done, how its quantity is stated, per which object, the unit, where, by whom and its type code). Code
checks every citation, then counts objects, multiplies, groups and prices.

Object kinds are a fixed English enum, so code matches a requirement's `per_object` to the object model without
reading any words.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from anbud.models import Citation

ObjectKind = Literal[
    "building", "stairwell", "floor", "apartment", "premises", "room", "kitchen", "bathroom", "wc", "other"
]
QuantitySource = Literal["stated", "per_object", "list_row", "lump_sum", "missing"]
Scope = Literal["included", "option", "excluded", "other_trade", "unclear"]
Level = Literal["exact", "visual", "interpretation"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class QuantityRecord(_Model):
    record_kind: Literal["requirement", "object"] = Field(
        description="object: a building object the documents list or count (one row of a room list, a stated total). "
        "requirement: something to install or do."
    )
    what: str = Field(min_length=1, max_length=200, description="Short description in the document's own words")
    type_code: str | None = Field(default=None, max_length=40, description="The type code as written, if any")
    object_kind: ObjectKind | None = Field(default=None, description="Required for object records")
    quantity: float | None = Field(
        default=None,
        ge=0,
        description="stated: the number written. per_object: the number per object (1 if the text implies one). "
        "list_row: how many objects the row is (leave out for one). Leave out for lump_sum and missing.",
    )
    quantity_source: QuantitySource = Field(
        description="stated: the quote writes the quantity. per_object: a number per object of the object model. "
        "list_row: an object record for one row of a list. lump_sum: one whole delivery with no count. "
        "missing: required, but no quantity is stated anywhere you can cite."
    )
    per_object: ObjectKind | None = Field(default=None, description="Required for per_object")
    unit: str = Field(min_length=1, max_length=20, description="The unit as written, or st for counts")
    location: str | None = Field(default=None, max_length=120, description="Room, floor or part, if stated")
    scope: Scope = Field(description="Who and whether: included in this contract, option, excluded, other trade")
    additive: bool = Field(
        default=False, description="True only if the quote says this comes on top of another stated quantity"
    )
    catalog_id: str | None = Field(
        default=None, max_length=80, description="The catalog item that fits, from the catalog list; leave out if none"
    )
    citations: list[Citation] = Field(min_length=1, max_length=3, description="One to three consecutive blocks")


class SetAside(_Model):
    block_ids: list[str] = Field(min_length=1, max_length=60)
    reason: str = Field(min_length=1, max_length=300)


class SubmitQuantitiesInput(_Model):
    """The tool's schema. Code validates each record and set-aside alone, so one bad record never sinks the rest."""

    records: list[QuantityRecord] = Field(description="Every quantity record in this chunk. Empty if none.")
    set_aside: list[SetAside] = Field(
        default_factory=list,
        description="Quantity lines (q=1) that are not requirements or objects of this contract, with the reason",
    )


class VerifiedRecord(_Model):
    """A record whose citations all resolved, numbered in the run."""

    id: str
    chunk_id: str
    record: QuantityRecord
    level: Level
    block_ids: list[str]


class Rejected(_Model):
    chunk_id: str
    reason: str
    block_ids: list[str] = Field(default_factory=list)


class Uncovered(_Model):
    """A quantity line no verified record used, with why: 'Ej omhändertaget'."""

    block_id: str
    reason: str
    set_aside_by_model: bool = False
