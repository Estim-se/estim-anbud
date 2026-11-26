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
    "building",
    "stairwell",
    "floor",
    "apartment",
    "premises",
    "room",
    "living_room",
    "bedroom",
    "half_room",
    "hall",
    "kitchen",
    "bathroom",
    "wc",
    "other",
]
QuantitySource = Literal["stated", "per_object", "list_row", "lump_sum", "missing"]
Scope = Literal["included", "option", "excluded", "other_trade", "unclear"]
Level = Literal["exact", "visual", "interpretation"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Contained(_Model):
    object_kind: ObjectKind
    quantity: float = Field(ge=0, description="How many in each counted object, as written")


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
        description="stated: the number written. per_object: the number per object if the text gives one (a digit or "
        "a number word; 1 for 'a' or 'one'), else leave out and code applies a recipe or marks it missing. list_row: "
        "how many objects the row is (leave out for one). Leave out for lump_sum and missing.",
    )
    quantity_source: QuantitySource = Field(
        description="stated: the quote writes the quantity. per_object: a number per object of the object model. "
        "list_row: an object record for one row of a list. lump_sum: one whole delivery with no count. "
        "missing: required, but no quantity is stated anywhere you can cite."
    )
    per_object: ObjectKind | None = Field(
        default=None, description="Required for per_object: the object kind or room type the quantity is per"
    )
    complete_installation: bool = Field(
        default=False,
        description="per_object only: true when the passage sets the whole electrical installation of each object "
        "(all its rooms) without listing items or counts. Leave quantity and catalog_id out; code applies the recipe "
        "room by room.",
    )
    unit: str = Field(min_length=1, max_length=20, description="The unit as written, or st for counts")
    location: str | None = Field(default=None, max_length=120, description="Room, floor or part, if stated")
    scope: Scope = Field(description="Who and whether: included in this contract, option, excluded, other trade")
    additive: bool = Field(
        default=False, description="True only if the quote says this comes on top of another stated quantity"
    )
    catalog_id: str | None = Field(
        default=None,
        max_length=80,
        description="The id of the catalog item that fits, from the catalog in the system prompt; leave out if none",
    )
    contains: list[Contained] = Field(
        default_factory=list,
        max_length=10,
        description="Object records only: what each counted object holds as the quote writes it, such as the rooms, "
        "kitchens and bathrooms of one apartment row",
    )
    citations: list[Citation] = Field(min_length=1, max_length=3, description="One to three consecutive blocks")


SetAsideKind = Literal[
    "heading_or_contents", "total_or_repeat", "other_trade", "not_this_contract", "no_requirement", "unclear"
]
# Swedish labels for the calc. Every kind but `unclear` is set aside with a reason; `unclear` stays "Ej omhändertaget".
SET_ASIDE_LABELS: dict[str, str] = {
    "heading_or_contents": "Rubrik, innehållsförteckning eller numrering",
    "total_or_repeat": "Summa eller upprepad uppgift",
    "other_trade": "Annan entreprenör eller beställaren",
    "not_this_contract": "Utanför denna entreprenad",
    "no_requirement": "Inget krav: information, villkor eller tidplan",
    "unclear": "Oklart: kan vara ett krav, men vad eller hur mycket framgår inte",
}


class SetAside(_Model):
    block_ids: list[str] = Field(min_length=1, max_length=60)
    kind: SetAsideKind = Field(
        description="heading_or_contents: a heading, a table-of-contents entry, a page, section or drawing number. "
        "total_or_repeat: a total, or a quantity stated again elsewhere. other_trade: work another contractor or the "
        "client does. not_this_contract: an object or part outside this contract. no_requirement: information, "
        "conditions, dates, areas or a schedule that asks for no work. unclear: it may be a requirement, but what or "
        "how much can't be told; code lists it as not handled."
    )
    reason: str = Field(min_length=1, max_length=300)


ColumnRole = Literal["id", "description", "type_code", "quantity", "unit", "location", "contains"]


class TableColumn(_Model):
    col: str = Field(min_length=1, max_length=4, description="The column as in the rows' cols attribute")
    role: ColumnRole = Field(
        description="id: the object's id or number (object tables). description: what the row is. quantity: the "
        "row's number. contains: a count of object_kind in each row's object (rooms, kitchens)."
    )
    object_kind: ObjectKind | None = Field(default=None, description="Required for contains")


class TableLayout(_Model):
    """A table code reads row by row: the model names the columns once, code reads every row in the range."""

    header_block_id: str = Field(description="The row with the column headings")
    first_block_id: str = Field(description="The first row code should read")
    last_block_id: str = Field(description="The last row code should read, in the same sheet or table")
    row_kind: Literal["object", "requirement"] = Field(
        description="object: each row is one object of object_kind (a room or apartment list). requirement: each "
        "row is a quantity to price (a schedule of quantities)."
    )
    object_kind: ObjectKind | None = Field(default=None, description="Required for object tables")
    unit: str | None = Field(default=None, max_length=20, description="The unit when no column gives it")
    scope: Scope = Field(description="As for records, for every row of the table")
    columns: list[TableColumn] = Field(min_length=1, max_length=12)


class SubmitQuantitiesInput(_Model):
    """The tool's schema. Code validates each record and set-aside alone, so one bad record never sinks the rest."""

    records: list[QuantityRecord] = Field(description="Every quantity record in this chunk. Empty if none.")
    set_aside: list[SetAside] = Field(
        default_factory=list,
        description="Quantity lines (q=1) and scope lines (s=1) that are not requirements or objects of this "
        "contract, with the reason",
    )
    tables: list[TableLayout] = Field(
        default_factory=list,
        description="Tables whose rows carry a cols attribute: name the columns once and code reads every row. "
        "Set aside heading, total and blank rows inside the range.",
    )


class Match(_Model):
    post_id: str = Field(min_length=1, max_length=40)
    catalog_id: str = Field(min_length=1, max_length=80, description="A catalog id from the list, or none")
    reason: str = Field(default="", max_length=300)


class SubmitMatchesInput(_Model):
    matches: list[Match] = Field(description="One entry per post in the request")


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
    """A quantity or scope line no verified record used, with why: 'Ej omhändertaget'."""

    block_id: str
    reason: str
    set_aside_by_model: bool = False
    set_aside_kind: SetAsideKind | None = None
    line: Literal["quantity", "scope"] = "quantity"

    @property
    def unresolved(self) -> bool:
        """Still "Ej omhändertaget": not set aside with a reason, or set aside as unclear."""
        return not self.set_aside_by_model or self.set_aside_kind == "unclear"
