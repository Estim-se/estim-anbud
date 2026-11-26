"""In-loop validation of `submit_flags` (plan §6.1 step 9). The tool loop calls `validate_flags` on the submitted list;
rejects go back to the lens once, with their reasons, through `repair_message`.

Checks, per flag:
- it parses as a `Flag`;
- every citation names a block in the corpus, and its quote is in that block after normalisation. A run with no
  tender blocks at all (the calc-only control arm of a planted variant) may cite calc posts instead: the citation names
  a post id and quotes that post's line as the prefix shows it. Every other run needs block citations. Line-break
  hyphenation is tolerated: prep joins a broken line with a space after the hyphen ("elin- stallation"), so a quote
  may give the word whole or as the block has it. A quote may skip text with "..." (each part must be there, in order);
- every number in the claim is in one of its quotes, apart from references to clauses, sections and standards;
- an absence finding lists the searches it ran, each one a `search_blocks` call in the lens's tool log, and the rule
  the absence is measured against.

Reasons name block ids, numbers and searches, never document text, so they are safe to store with the lens result.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping
from typing import Any

from pydantic import ValidationError

from anbud.granskning.contracts import CalcPost, ToolCall
from anbud.granskning.corpus import Corpus
from anbud.granskning.prefix import post_line
from anbud.models import Flag
from anbud.prep.text import normalise

MIN_QUOTE_CHARS = 4
_HYPHEN_BREAK = re.compile(r"(?<=\w)- (?=\w)")
_ELLIPSIS = re.compile(r"\.{3,}")
# References whose numbers need not be quoted: sections, chapters and coded headings (AFC.32)...
_CLAUSE_REFS = r"§\s*\d+[a-z]?(?:\s*(?:st\.?|stycket|mom\.?|p\.?|punkt)\s*\d+)*|\b[Kk]ap(?:itel|\.)?\s*\d+"
_CODE_REFS = r"\b[A-ZÅÄÖ]{1,5}\.\d[0-9A-ZÅÄÖ./]*"

# ...and standards or regulations by name and number. Case-sensitive, so "en 5" stays a number.
_CONTRACT_REFS = r"\b(?:AB|ABT|ABK|AB-U|ABT-U|AMA|MER|RA)\s*-?\s*\d{2,4}\b"

_REGULATION_REFS = (
    r"\b(?:BFS|ELSÄK-FS|AFS|SFS|BBR|PBF)\s*\d{4}:\d+|\b(?:SS|SS-EN|EN|IEC|HD)(?:[\s-]+\d+(?:[.:\-]\d+)*)+"
)

_REFERENCES = re.compile("|".join((_CLAUSE_REFS, _CODE_REFS, _CONTRACT_REFS, _REGULATION_REFS)))
_NUMBER = re.compile(r"\d+(?:[   ]\d{3})*(?:[.,]\d+)?")


def _match_form(text: str) -> str:
    return _HYPHEN_BREAK.sub("", normalise(text)).casefold()


def quote_matches(quote: str, block_text: str) -> bool:
    """The quote is in the block after normalisation, tolerating line-break hyphenation and "..." skips."""
    q = normalise(quote)
    if len(q) < MIN_QUOTE_CHARS:
        return False
    block = normalise(block_text).casefold()
    joined = _HYPHEN_BREAK.sub("", block)
    parts = [p.strip() for p in _ELLIPSIS.split(q.casefold())]
    parts = [p for p in parts if p]
    if not parts:
        return False
    for haystack, transform in ((block, lambda s: s), (joined, lambda s: _HYPHEN_BREAK.sub("", s))):
        pos = 0
        for part in parts:
            found = haystack.find(transform(part), pos)
            if found < 0:
                break
            pos = found + len(transform(part))
        else:
            return True
    return False


def _number_forms(token: str) -> set[str]:
    compact = re.sub(r"[   ]", "", token)
    forms = {compact, re.sub(r"\D", "", compact)}
    try:
        value = float(compact.replace(",", "."))
        forms.add(repr(value))
    except ValueError:
        pass
    # "1 200" may also be two numbers that happen to sit together
    forms.update(re.split(r"[   ]", token))
    return forms


def claim_numbers(claim: str) -> list[str]:
    """The numbers in a claim that a quote must carry: everything except clause, section and standard references."""
    text = _REFERENCES.sub(" ", normalise(claim))
    return [m.group(0) for m in _NUMBER.finditer(text)]


def _numbers_in(texts: list[str]) -> set[str]:
    found: set[str] = set()
    for text in texts:
        for m in _NUMBER.finditer(normalise(text)):
            found.update(_number_forms(m.group(0)))
    return found


def _search_key(query: str) -> str:
    return normalise(query).casefold()


def searches_run(tool_log: list[ToolCall]) -> set[str]:
    """The search_blocks queries the loop ran (not refused, not failed), normalised for comparison."""
    return {
        _search_key(str(call.input.get("query", "")))
        for call in tool_log
        if call.tool == "search_blocks" and call.refused is None and call.error is None
    }


def cited_text(citation_id: str, corpus: Corpus, calc_posts: Mapping[str, CalcPost] | None = None) -> str | None:
    """The text a citation's quote must come from: its block's, or, only when the corpus has no blocks at all, the
    calc post's line."""
    block = corpus.get(citation_id)
    if block is not None:
        return block.text
    if calc_posts and not corpus.blocks and citation_id in calc_posts:
        return post_line(calc_posts[citation_id])
    return None


def check_flag(
    flag: Flag,
    searches: set[str],
    corpus: Corpus,
    claim_types: Collection[str] | None = None,
    calc_posts: Mapping[str, CalcPost] | None = None,
) -> list[str]:
    reasons: list[str] = []
    if claim_types is not None and flag.claim_type not in claim_types:
        reasons.append(f"claim_type {flag.claim_type!r} is not one of the claim types in the system prompt")
    for i, citation in enumerate(flag.citations):
        text = cited_text(citation.block_id, corpus, calc_posts)
        if text is None:
            reasons.append(f"citation {i + 1}: block {citation.block_id} does not exist")
        elif not quote_matches(citation.quote, text):
            reasons.append(f"citation {i + 1}: the quote is not in block {citation.block_id}")
    quoted = _numbers_in([c.quote for c in flag.citations])
    missing = [n for n in claim_numbers(flag.claim) if not (_number_forms(n) & quoted)]
    if missing:
        reasons.append("numbers in the claim that no quote contains: " + ", ".join(dict.fromkeys(missing)))
    if flag.absence:
        if not flag.searches:
            reasons.append("an absence finding must list the searches it ran")
        not_run = [s for s in flag.searches if _search_key(s) not in searches]
        if not_run:
            reasons.append("searches listed that this lens never ran: " + "; ".join(repr(s) for s in not_run))
        if not (flag.reference_rule or "").strip():
            reasons.append("an absence finding must name the reference rule it is measured against")
    return reasons


def _schema_reasons(error: ValidationError) -> list[str]:
    return [f"{'.'.join(str(p) for p in e['loc']) or 'flag'}: {e['msg']}" for e in error.errors(include_input=False)]


def _reject(index: int, reasons: list[str], item: Any) -> dict[str, Any]:
    return {"index": index, "reasons": reasons, "reason": "; ".join(reasons), "flag": item}


def flag_validator(
    corpus: Corpus, claim_types: Collection[str] | None = None, calc_posts: Mapping[str, CalcPost] | None = None
) -> Callable[[list[dict[str, Any]], list[ToolCall]], tuple[list[Flag], list[dict[str, Any]]]]:
    """`validate_flags` bound to a corpus, in the shape the tool loop's `Validator` takes."""

    def validator(raw: list[dict[str, Any]], tool_log: list[ToolCall]) -> tuple[list[Flag], list[dict[str, Any]]]:
        return validate_flags(raw, tool_log, corpus, claim_types, calc_posts)

    return validator


def validate_flags(
    raw: list[dict[str, Any]],
    tool_log: list[ToolCall],
    corpus: Corpus,
    claim_types: Collection[str] | None = None,
    calc_posts: Mapping[str, CalcPost] | None = None,
) -> tuple[list[Flag], list[dict[str, Any]]]:
    """The flags that pass every check, and the rejects as {"index", "reasons", "reason", "flag"}: `reasons` a list,
    `reason` the same joined (the tool loop's repair message reads it), `flag` the raw input. With `claim_types`, a
    flag's claim type must be one of them."""
    searches = searches_run(tool_log)
    valid: list[Flag] = []
    rejects: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        try:
            flag = Flag.model_validate(item)
        except ValidationError as error:
            rejects.append(_reject(index, _schema_reasons(error), item))
            continue
        reasons = check_flag(flag, searches, corpus, claim_types, calc_posts)
        if reasons:
            rejects.append(_reject(index, reasons, item))
        else:
            valid.append(flag)
    return valid, rejects


def repair_message(rejects: list[dict[str, Any]]) -> str:
    """The tool_result text for the one repair turn: which flags failed and why. Resubmit all flags, fixed."""
    lines = [
        f"{len(rejects)} flag(s) failed the checks. Call submit_flags again with every flag, the failed ones fixed or "
        "left out. Quotes must be copied exactly from the block; numbers in a claim must appear in its quotes."
    ]
    for r in rejects:
        lines.append(f"- flag {r['index'] + 1}: " + "; ".join(r["reasons"]))
    return "\n".join(lines)
