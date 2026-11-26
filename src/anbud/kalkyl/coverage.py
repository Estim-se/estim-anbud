"""Line-level coverage (#73 step 4): every quantity line and every scope line is used by a verified record, or listed
with a reason.

Reasons, in order: set aside by the model (with its reason), not read (the chunk's call failed, ran out of budget or
was stopped), not read to the end (the answer hit `max_tokens`), no answer, cited only by a rejected record, or
nothing handled it. Nothing is silently dropped. A row the object list read by code counts (`object_list`) is used by
the object model, cited by a record or not.
"""

from __future__ import annotations

from anbud.kalkyl.extract import ChunkResult
from anbud.kalkyl.lines import Chunk
from anbud.kalkyl.records import Uncovered, VerifiedRecord

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
) -> list[Uncovered]:
    used = {b for v in verified for b in v.block_ids} | (counted or set())
    out: list[Uncovered] = []
    for chunk, res in zip(chunks, results, strict=True):
        aside = {b: s.reason for s in res.set_aside for b in s.block_ids}
        rejected = {b: r.reason for r in res.rejected for b in r.block_ids}
        lines = [(b, "quantity") for b in chunk.quantity_ids] + [(b, "scope") for b in chunk.scope_ids]
        for block_id, line in lines:
            if block_id in used:
                continue
            if block_id in aside:
                reason = f"Bortvalt av modellen: {aside[block_id]}"
                out.append(Uncovered(block_id=block_id, reason=reason, set_aside_by_model=True, line=line))
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
