"""Coverage by code group: every AMA or AF code group in the technical description and the AF maps to a calc post or
to a line set aside with a cited reason.

Line coverage ("Ej omhändertaget") sees only the lines the line finder marks. A whole group of work can go missing
without any marked line: its text is prose the finder doesn't mark, or no record ever used it. This check reads the
documents' coded headings instead. A group is covered when a block under it, or under a code below it, is cited by a
post's record, or is a line set aside with a reason. The sheet lists the topmost groups that are neither: a group
whose sub-codes are all uncovered is one row, so a missing category shows once.

Codes come from the headings in the outline: AF codes (AFB, AFC.11), codes of two digits and letters (63.CB, 63.CB/1)
and letter codes with a number (YGB.1). A heading that starts with an upper-case word is no code. The hierarchy comes
from the codes themselves (AFC.111 is under AFC.11, AFC.1 and AFC; 63.CBB under 63.CB and 63.C; YGB.1 under YGB and
YG), so a group with no heading of its own still gathers its sub-codes. Documents: the AF, the electrical documents
and the unclassified ones (so a technical description with no name signal is still checked); not the contract, the
general description, the time plan or the lists.

Code numbers only: a group is named by its code, its document, and its first heading's block id and page, never by the
heading's words, so no AMA title reaches Estim's output.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from anbud.granskning.corpus import Corpus
from anbud.selection import classify, heading_code, views

AF = "af"
TECHNICAL = "teknisk"
SECTIONS = {TECHNICAL: "Teknisk beskrivning", AF: "AF"}
_AF = re.compile(r"AF[A-ZÅÄÖ](?:\.\d{1,4})?")
_DIGITS = re.compile(r"\d{2}\.[A-ZÅÄÖ]{1,4}(?:/\d{1,3})?")
_LETTERS = re.compile(r"[A-ZÅÄÖ]{2,4}(?:\.\d{1,4})+")
CHECKED_ROLES = frozenset({"af", "el_description", "el", "el_list", "el_drawing", None})


def group_code(title: str) -> str | None:
    """The AMA or AF code a heading starts with, or None."""
    code = heading_code(title)
    if code and any(p.fullmatch(code) for p in (_AF, _DIGITS, _LETTERS)):
        return code
    return None


def parent(code: str) -> str | None:
    """The code one level up: AFC.11 -> AFC.1 -> AFC; 63.CB/1 -> 63.CB -> 63.C; YGB.12 -> YGB.1 -> YGB -> YG."""
    if "/" in code:
        return code.split("/", 1)[0]
    head, dot, tail = code.rpartition(".")
    if dot:
        if len(tail) > 1:
            return f"{head}.{tail[:-1]}"
        return head if tail.isdigit() else None  # 63.C is a top: two digits alone group too much
    if code.startswith("AF") or len(code) <= 2:
        return None
    return code[:-1]


def section(code: str) -> str:
    return AF if code.startswith("AF") else TECHNICAL


@dataclass
class CodeGroup:
    code: str
    section: str
    doc_id: str
    block_id: str
    page: int | None
    codes: int = 0  # coded headings in the group, its own included
    blocks: int = 0  # text blocks under it
    lines: int = 0  # of them, lines the line finder marked (quantity or scope)
    open_lines: int = 0  # of those, still "Ej omhändertaget"
    post: bool = False
    reason: bool = False

    @property
    def covered(self) -> bool:
        return self.post or self.reason


@dataclass
class CodeCoverage:
    codes: dict[str, CodeGroup] = field(default_factory=dict)  # every coded heading's group, its sub-codes rolled in
    missing: list[CodeGroup] = field(default_factory=list)  # the topmost groups with neither a post nor a reason

    def counts(self, which: str | None = None) -> dict[str, int]:
        groups = [g for g in self.codes.values() if which is None or g.section == which]
        return {
            "codes": len(groups),
            "with_post": sum(g.post for g in groups),
            "with_reason_only": sum(g.reason and not g.post for g in groups),
            "without": sum(not g.covered for g in groups),
            "rows": sum(1 for g in self.missing if which is None or g.section == which),
        }

    def summary(self) -> dict[str, Any]:
        """Counts only: safe to print and to log."""
        return {name: self.counts(name) for name in SECTIONS}


@dataclass
class _Node:
    code: str
    first: tuple[str, str, int | None, int] | None = None  # doc, heading block, page, reading position
    own: set[str] = field(default_factory=set)  # blocks whose nearest coded heading is this code, headings included
    present: bool = False


def coverage(
    corpus: Corpus, posted: set[str], reasoned: set[str], lines: set[str], open_lines: set[str]
) -> CodeCoverage:
    """Each code group's coverage. `posted`: blocks a post's records cite; `reasoned`: lines set aside with a reason;
    `lines`: the line finder's quantity and scope lines; `open_lines`: lines still not handled."""
    roles = {v.doc_id: classify(v).role for v in views(corpus)}
    starts = {o.block_id: o for o in corpus.outline if o.block_id}
    nodes: dict[str, _Node] = {}
    stacks: dict[str, list[tuple[int, str | None]]] = {}
    for position, block_id in enumerate(corpus.order):
        block = corpus.blocks[block_id]
        if block.doc_id in corpus.excluded or roles.get(block.doc_id) not in CHECKED_ROLES:
            continue
        stack = stacks.setdefault(block.doc_id, [])
        entry = starts.get(block_id)
        if entry is not None:
            while stack and stack[-1][0] >= entry.level:
                stack.pop()
            code = group_code(entry.title)
            stack.append((entry.level, code))
            if code is not None:
                node = nodes.setdefault(code, _Node(code))
                node.present = True
                node.first = node.first or (block.doc_id, block_id, block.page, position)
        nearest = next((code for _, code in reversed(stack) if code is not None), None)
        if nearest is not None:
            nodes[nearest].own.add(block_id)
    return _roll_up(corpus, nodes, posted, reasoned, lines, open_lines)


def _roll_up(
    corpus: Corpus,
    nodes: dict[str, _Node],
    posted: set[str],
    reasoned: set[str],
    lines: set[str],
    open_lines: set[str],
) -> CodeCoverage:
    for code in list(nodes):  # codes with no heading of their own, so their sub-codes group under them
        up = parent(code)
        while up is not None and up not in nodes:
            nodes[up] = _Node(up)
            up = parent(up)
    children: dict[str, list[str]] = {}
    for code in nodes:
        if (up := parent(code)) is not None:
            children.setdefault(up, []).append(code)

    groups: dict[str, CodeGroup] = {}

    def build(code: str) -> CodeGroup:
        node = nodes[code]
        kids = [build(k) for k in children.get(code, [])]
        firsts = [n for n in (node.first, *(nodes[k.code].first for k in kids)) if n is not None]
        doc_id, block_id, page, position = min(firsts, key=lambda f: f[3])
        node.first = node.first or (doc_id, block_id, page, position)
        text = [b for b in node.own if corpus.blocks[b].kind != "heading"]
        group = CodeGroup(
            code=code,
            section=section(code),
            doc_id=doc_id,
            block_id=block_id,
            page=page,
            codes=int(node.present) + sum(k.codes for k in kids),
            blocks=len(text) + sum(k.blocks for k in kids),
            lines=len(lines & node.own) + sum(k.lines for k in kids),
            open_lines=len(open_lines & node.own) + sum(k.open_lines for k in kids),
            post=bool(posted & node.own) or any(k.post for k in kids),
            reason=bool(reasoned & node.own) or any(k.reason for k in kids),
        )
        groups[code] = group
        return group

    tops = [code for code in nodes if parent(code) is None]
    for code in tops:
        build(code)
    out = CodeCoverage(codes={c: g for c, g in groups.items() if nodes[c].present})
    for code in sorted(groups, key=lambda c: (nodes[c].first[3] if nodes[c].first else 0, c)):
        group, up = groups[code], parent(code)
        if not group.covered and (up is None or groups[up].covered):
            while not nodes[code].present and len(children.get(code, [])) == 1:  # a code group of one is that code
                code = children[code][0]
            out.missing.append(groups[code])
    return out


def for_result(result: Any, corpus: Corpus) -> CodeCoverage:
    """The coverage of a Kalkyl result (`KalkylResult`) over the corpus it read."""
    posted = {b for p in result.posts for ln in p.lines for b in ln.record.block_ids}
    reasoned = {u.block_id for u in result.uncovered if not u.unresolved}
    open_lines = {u.block_id for u in result.uncovered if u.unresolved}
    lines = set(_ids(c.quantity_ids for c in result.chunks)) | set(_ids(c.scope_ids for c in result.chunks))
    return coverage(corpus, posted, reasoned, lines, open_lines)


def _ids(groups: Iterable[list[str]]) -> Iterable[str]:
    for ids in groups:
        yield from ids
