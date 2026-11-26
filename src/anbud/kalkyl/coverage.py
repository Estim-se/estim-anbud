"""Line-level coverage (#73 step 4): every quantity line, scope line and not-included line is used by a verified
record, or listed with a reason.

Reasons, in order: set aside by the model (with its kind and reason; of the kinds only `unclear` stays unresolved),
not read (the chunk's call failed, ran out of budget or was stopped), not read to the end (the answer hit
`max_tokens`), no answer, cited only by a rejected record, or nothing handled it. Nothing is silently dropped. A row
the object list read by code counts (`object_list`) is used by the object model, cited by a record or not.

A not-included line (`lines.not_included_line_ids`) that no record uses and the model didn't set aside is set aside by
code: its own words say it isn't part of the contract, so it is listed under "Bortvalt med skäl" with its citation and
a request to check it, never left out and never priced.

A line the model set aside as another trade's work (`other_trade`) needs its citation to say so: its own text or one of
the two headings above it names another trade or its contractor, the client's side, or a contract boundary
(`lines.other_party_line_ids`). Without that, the model's judgement is an inference from what the line is about, and an
inference can drop work this contract does (a feed or a connection to another trade's equipment). Such a line stays
"Ej omhändertaget" as unclear, with the model's reason and a request to check who does the work.
"""

from __future__ import annotations

from anbud.kalkyl.extract import ChunkResult
from anbud.kalkyl.lines import Chunk
from anbud.kalkyl.records import SET_ASIDE_LABELS, Uncovered, VerifiedRecord

OTHER_TRADE_CHECK = (
    "Kontrollera: modellen bedömde raden som annan entreprenörs arbete, men raden nämner ingen annan entreprenör, "
    "beställaren eller en entreprenadgräns. Modellens skäl: {reason}"
)
NOT_INCLUDED_REASON = (
    "Bortvalt av kod (Utanför denna entreprenad): raden säger att något inte ingår. "
    "Kontrollera att inget på raden ska prissättas"
)
_NOT_READ = {
    "budget": "Ej läst: körningens budget tog slut",
    "stopped": "Ej läst: körningen stoppades",
    "replay_miss": "Ej läst: svaret finns inte i stegcachen",
}


def uncovered_lines(
    chunks: list[Chunk],
    results: list[ChunkResult],
    verified: list[VerifiedRecord],
    counted: set[str] | None = None,
    not_included: set[str] | None = None,
    other_party: set[str] | None = None,
) -> list[Uncovered]:
    """`other_party`, when given, is the lines that name another party to the work; an `other_trade` set-aside of any
    other line stays unresolved."""
    used = {b for v in verified for b in v.block_ids} | (counted or set())
    not_included = not_included or set()
    out: list[Uncovered] = []
    for chunk, res in zip(chunks, results, strict=True):
        aside = {b: s for s in res.set_aside for b in s.block_ids}
        rejected = {b: r.reason for r in res.rejected for b in r.block_ids}
        lines = [(b, "quantity") for b in chunk.quantity_ids] + [(b, "scope") for b in chunk.scope_ids]
        marked = {b for b, _ in lines}
        lines += [(b, "not_included") for b in chunk.block_ids if b in not_included and b not in marked]
        for block_id, line in lines:
            if block_id in used:
                continue
            if block_id in aside:
                item = aside[block_id]
                if item.kind == "other_trade" and other_party is not None and block_id not in other_party:
                    out.append(
                        Uncovered(
                            block_id=block_id,
                            reason=OTHER_TRADE_CHECK.format(reason=item.reason),
                            set_aside_by_model=True,
                            set_aside_kind="unclear",
                            other_trade_unchecked=True,
                            line=line,
                        )
                    )
                    continue
                reason = f"Bortvalt av modellen ({SET_ASIDE_LABELS[item.kind]}): {item.reason}"
                out.append(
                    Uncovered(
                        block_id=block_id, reason=reason, set_aside_by_model=True, set_aside_kind=item.kind, line=line
                    )
                )
                continue
            if block_id in not_included:
                out.append(
                    Uncovered(
                        block_id=block_id,
                        reason=NOT_INCLUDED_REASON,
                        set_aside_by_code=True,
                        set_aside_kind="not_this_contract",
                        line="not_included" if line == "not_included" else line,
                    )
                )
                continue
            if res.status == "not_run":
                reason = _NOT_READ.get(res.error or "", "Ej läst")
            elif res.status == "error":
                reason = f"Ej läst: anropet misslyckades ({res.error})"
            elif res.status == "truncated":
                reason = "Ej läst klart: svaret nådde max_tokens"
            elif res.status == "no_submit":
                reason = "Modellen lämnade inget svar för avsnittet"
            elif block_id in res.unread_rows:
                reason = f"Tabellrad kunde inte läsas: {res.unread_rows[block_id]}"
            elif block_id in rejected:
                reason = f"Citatet underkändes: {rejected[block_id]}"
            elif line == "scope":
                reason = "Omfattningstext utan post: ingen post använder stycket"
            else:
                reason = "Ej omhändertaget i underlaget: ingen post eller objekt använder raden"
            out.append(Uncovered(block_id=block_id, reason=reason, line=line))
    return out
