"""Held-out split of a company's tenders, decided before anyone opens a file.

Tenders that share a client or main contractor, a property owner or a project family form one group (union-find), so
shared documents such as a property owner's handbook can't leak across the split through a second client. Each new
group is assigned by a seeded hash: 60% dev, 40% held-out. Assignments are appended to a ledger and never recomputed.

- A group with a forced-dev tender (the first development tender, for example) goes to dev.
- A new tender that joins a dev group and a held-out group moves the held-out side to dev (a burn, logged).
- Corrected group attributes, or forced dev switched on later, are logged as an update. If that joins a held-out group
  to a dev one, the held-out side is burned too. Nothing ever moves from dev to held-out.
- Prospective tenders get their own status and don't move anyone else.
- The client or main contractor is required (except for prospective tenders), and placeholders such as "N/A" are
  refused: a blank cell is the only way to say "unknown".

The ledger holds neutral tender ids, statuses and hashed group keys only. The screen gets counts only.
The seed is committed on purpose; this file is code-owned.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import os
import re
import sys
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from anbud import paths

SEED = "anbud-split-v1-1ac7f4dab08903d7bbb60582a64b0f98"
DEV_SHARE_PERCENT = 60

DEV = "dev"
HELDOUT = "heldout"
PROSPECTIVE = "prospective"
STATUSES = frozenset({DEV, HELDOUT, PROSPECTIVE})

FIELDS = ("tender_id", "client", "property_owner", "project_family", "prospective", "forced_dev")
GROUP_COLUMNS = ("client", "property_owner", "project_family")
# Legal-form words dropped from either end of a name, after accents are folded. Multi-word forms first.
_LEGAL_SUFFIXES = (
    ("ekonomisk", "forening"),
    ("ek", "for"),
    ("a", "b"),
    ("h", "b"),
    ("k", "b"),
    ("ab",),
    ("aktiebolag",),
    ("aktiebolaget",),
    ("publ",),
    ("hb",),
    ("handelsbolag",),
    ("handelsbolaget",),
    ("kb",),
    ("kommanditbolag",),
    ("kommanditbolaget",),
)
# Values that mean "unknown". A blank cell says that; these would wrongly join unrelated tenders. A value is refused if
# it is one of _PLACEHOLDERS, or made only of _PLACEHOLDER_WORDS, or only of legal forms ("AB").
_PLACEHOLDERS = frozenset(
    {"n a", "na", "x", "xx", "0", "00", "e t", "ej tillampligt", "ej aktuellt", "se ovan", "samma som ovan"}
)
_PLACEHOLDER_WORDS = frozenset(
    {"nan", "null", "none", "unknown", "tbd", "tba", "xxx", "okand", "okant", "kand", "kant", "saknas", "ej", "angivet",
     "angiven", "ingen", "inget", "uppgift", "uppgifter", "vet", "inte", "privat", "privatperson", "privatpersoner",
     "person", "kund", "bestallare", "fastighetsagare", "agare", "projekt", "ovrigt", "diverse", "div"}
)  # fmt: skip
_LEGAL_FORM_WORDS = frozenset(w for form in _LEGAL_SUFFIXES for w in form)
_TOKEN = re.compile(r"[^\W_]+")
_TRUE = {"yes", "y", "ja", "j", "true", "1", "x"}
_FALSE = {"", "no", "n", "nej", "false", "0"}


class SplitError(ValueError):
    """A problem with the metadata or the ledger. Messages never contain names."""


@dataclass(frozen=True)
class TenderRow:
    tender_id: str
    keys: tuple[str, ...]  # hashed group keys
    prospective: bool = False
    forced_dev: bool = False


@dataclass
class LedgerState:
    status: dict[str, str] = field(default_factory=dict)
    keys: dict[str, tuple[str, ...]] = field(default_factory=dict)
    prospective: dict[str, bool] = field(default_factory=dict)
    forced_dev: dict[str, bool] = field(default_factory=dict)

    def is_prospective(self, tender_id: str) -> bool:
        return self.prospective.get(tender_id, self.status.get(tender_id) == PROSPECTIVE)

    def is_forced(self, tender_id: str) -> bool:
        return self.forced_dev.get(tender_id, False)


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", text).casefold())
    return "".join(c for c in text if not unicodedata.combining(c))


def _strip_legal_forms(tokens: list[str]) -> list[str]:
    changed = True
    while tokens and changed:
        changed = False
        for form in _LEGAL_SUFFIXES:
            n = len(form)
            if len(tokens) > n and tuple(tokens[-n:]) == form:
                del tokens[-n:]
                changed = True
            if len(tokens) > n and tuple(tokens[:n]) == form:
                del tokens[:n]
                changed = True
    return tokens


def normalise(name: str) -> str:
    """Casefold, fold accents, and drop legal forms: 'Fiktiva Bygg i Norr A.B.' == 'fiktiva bygg i norr'."""
    return " ".join(_strip_legal_forms(_TOKEN.findall(_fold(name))))


def hash_key(key: str) -> str:
    return hashlib.sha256(f"{SEED}\x1f{key}".encode()).hexdigest()[:24]


def _check_placeholder(value: str, column: str, line: int | None) -> None:
    """Refuse a placeholder, also with a legal form attached ("Okänd AB")."""
    raw = _TOKEN.findall(_fold(value))
    if not raw:
        return
    stripped = normalise(value).split()
    if (
        all(w in _LEGAL_FORM_WORDS for w in raw)
        or any(" ".join(words) in _PLACEHOLDERS for words in (raw, stripped))
        or any(words and all(w in _PLACEHOLDER_WORDS for w in words) for words in (raw, stripped))
    ):
        where = f"metadata line {line}: " if line is not None else ""
        raise SplitError(f"{where}column {column} holds a placeholder; write the real name or leave the cell blank")


def group_keys(client: str, property_owner: str, project_family: str, line: int | None = None) -> tuple[str, ...]:
    keys = []
    # The same organisation as client in one tender and owner in another is the same group.
    for column, value in (("client", client), ("property_owner", property_owner)):
        _check_placeholder(value or "", column, line)
        norm = normalise(value or "")
        if norm:
            keys.append(hash_key("entity:" + norm))
    _check_placeholder(project_family or "", "project_family", line)
    family = normalise(project_family or "")
    if family:
        keys.append(hash_key("family:" + family))
    return tuple(sorted(set(keys)))


def bucket(canonical_key: str) -> int:
    return int(hashlib.sha256(f"{SEED}\x1ebucket\x1e{canonical_key}".encode()).hexdigest()[:8], 16) % 100


def _parse_bool(value: str | None, column: str, line: int) -> bool:
    text = (value or "").strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise SplitError(f"metadata line {line}: column {column} must be yes or no")


def read_metadata(path: Path) -> list[TenderRow]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = [f for f in FIELDS if f not in (reader.fieldnames or [])]
        if missing:
            raise SplitError(f"metadata is missing columns: {', '.join(missing)}")
        rows = []
        for line, raw in enumerate(reader, start=2):
            tender_id = (raw["tender_id"] or "").strip()
            try:
                paths.check_tender_id(tender_id)
            except ValueError as exc:
                raise SplitError(f"metadata line {line}: {exc}") from None
            prospective = _parse_bool(raw["prospective"], "prospective", line)
            forced_dev = _parse_bool(raw["forced_dev"], "forced_dev", line)
            if prospective and forced_dev:
                raise SplitError(f"metadata line {line}: a tender can't be both prospective and forced dev")
            keys = group_keys(raw["client"], raw["property_owner"], raw["project_family"], line)
            if not prospective and not normalise(raw["client"] or ""):
                raise SplitError(f"metadata line {line}: client or main contractor is required")
            rows.append(TenderRow(tender_id, keys, prospective, forced_dev))
    return rows


def _event_error(number: int, what: str) -> SplitError:
    return SplitError(
        f"ledger line {number}: {what}. Nothing runs until it is fixed; remove only a broken last line, and never "
        "delete the ledger (a new one would split again from scratch)"
    )


def _valid_keys(keys: object) -> bool:
    return isinstance(keys, list) and all(isinstance(k, str) and re.fullmatch(r"[0-9a-f]{24}", k) for k in keys)


def load_ledger(path: Path) -> LedgerState:
    state = LedgerState()
    if path.is_symlink():
        raise SplitError("the ledger is a symlink; it must be a plain file in the company folder")
    if not path.exists():
        return state
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            kind, tender = event["event"], event["tender"]
            if not isinstance(tender, str) or not paths.TENDER_ID.fullmatch(tender):
                raise _event_error(number, "bad tender id")
            if kind in ("assign", "update") and not _valid_keys(event["keys"]):
                raise _event_error(number, "bad keys")
            for flag in ("prospective", "forced_dev"):
                if flag in event and not isinstance(event[flag], bool):
                    raise _event_error(number, f"{flag} must be true or false")
            if kind == "assign":
                if tender in state.status:
                    raise _event_error(number, "tender assigned twice")
                if event["status"] not in STATUSES:
                    raise _event_error(number, "unknown status")
                state.status[tender] = event["status"]
                state.keys[tender] = tuple(event["keys"])
                prospective = event.get("prospective", event["status"] == PROSPECTIVE)
                forced_dev = event.get("forced_dev", False)
                if prospective != (event["status"] == PROSPECTIVE) or (forced_dev and event["status"] != DEV):
                    raise _event_error(number, "flags contradict the status")
                state.prospective[tender] = prospective
                state.forced_dev[tender] = forced_dev
            elif kind == "burn":
                if (event["from"], event["to"]) != (HELDOUT, DEV) or state.status.get(tender) != HELDOUT:
                    raise _event_error(number, "burn doesn't match the current status")
                state.status[tender] = DEV
            elif kind == "update":
                if tender not in state.status:
                    raise _event_error(number, "update for a tender that isn't assigned")
                state.keys[tender] = tuple(event["keys"])
                state.forced_dev[tender] = bool(event["forced_dev"])
            else:
                raise _event_error(number, "unknown event")
        except SplitError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            raise _event_error(number, "unreadable") from None
    return state


def status_of(company_id: str, tender_id: str, root: Path | None = None) -> str | None:
    return load_ledger(paths.ledger_path(company_id, root)).status.get(tender_id)


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, item: str) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: str) -> str:
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def plan_split(state: LedgerState, rows: Iterable[TenderRow]) -> list[dict]:
    """Return the new ledger events for these rows. Pure: it reads the state and changes nothing."""
    new: dict[str, TenderRow] = {}
    seen: set[str] = set()
    keys = dict(state.keys)
    forced = {t: state.is_forced(t) for t in state.status}
    touched: set[str] = set()
    events: list[dict] = []
    updates: list[dict] = []  # written last: if an append is cut off, the next run re-derives them and burns again
    for row in rows:
        if row.tender_id in seen:
            raise SplitError("a tender id appears twice in the metadata")
        seen.add(row.tender_id)
        if row.prospective and row.forced_dev:
            raise SplitError("a tender can't be both prospective and forced dev")
        tender = row.tender_id
        if tender not in state.status:
            new[tender] = row
            continue
        if row.prospective != state.is_prospective(tender):
            raise SplitError("a tender's prospective flag changed after assignment; that needs a manual decision")
        if state.is_forced(tender) and not row.forced_dev:
            raise SplitError("forced dev was switched off for an assigned tender; nothing moves back, so fix the row")
        if row.keys != state.keys[tender] or row.forced_dev != state.is_forced(tender):
            updates.append(
                {
                    "event": "update",
                    "tender": tender,
                    "old_keys": list(state.keys[tender]),
                    "keys": list(row.keys),
                    "forced_dev": row.forced_dev,
                }
            )
            keys[tender] = row.keys
            forced[tender] = row.forced_dev
            touched.add(tender)

    for tender_id in sorted(t for t, r in new.items() if r.prospective):
        events.append(_assign(tender_id, PROSPECTIVE, "prospective", new[tender_id].keys, prospective=True))

    members = {t: keys[t] for t in state.status if not state.is_prospective(t)}
    members.update({t: r.keys for t, r in new.items() if not r.prospective})
    uf = _UnionFind()
    first_with_key: dict[str, str] = {}
    for tender_id in sorted(members):
        uf.add(tender_id)
        for key in members[tender_id]:
            if key in first_with_key:
                uf.union(tender_id, first_with_key[key])
            else:
                first_with_key[key] = tender_id

    components: dict[str, list[str]] = {}
    for tender_id in sorted(members):
        components.setdefault(uf.find(tender_id), []).append(tender_id)

    for component in components.values():
        fresh = [t for t in component if t in new]
        changed = [t for t in component if t in touched]
        if not fresh and not changed:
            continue
        existing = [t for t in component if t not in new]
        statuses = {state.status[t] for t in existing}
        is_forced = any(new[t].forced_dev for t in fresh) or any(forced[t] for t in changed)
        if is_forced or DEV in statuses:
            target, reason = DEV, ("forced_dev" if is_forced else "group")
            burn_reason = "forced_dev" if is_forced else ("bridge" if fresh else "update")
            for tender_id in existing:
                if state.status[tender_id] == HELDOUT:
                    events.append(_burn(tender_id, burn_reason))
        elif HELDOUT in statuses:
            target, reason = HELDOUT, "group"
        else:
            all_keys = sorted({k for t in component for k in members[t]})
            canonical = all_keys[0] if all_keys else "tender:" + min(component)
            target = DEV if bucket(canonical) < DEV_SHARE_PERCENT else HELDOUT
            reason = "hash"
        for tender_id in fresh:
            events.append(_assign(tender_id, target, reason, members[tender_id], forced_dev=new[tender_id].forced_dev))
    return events + updates


def _assign(
    tender_id: str,
    status: str,
    reason: str,
    keys: tuple[str, ...],
    *,
    prospective: bool = False,
    forced_dev: bool = False,
) -> dict:
    return {
        "event": "assign",
        "tender": tender_id,
        "status": status,
        "reason": reason,
        "keys": list(keys),
        "prospective": prospective,
        "forced_dev": forced_dev,
    }


def _burn(tender_id: str, reason: str) -> dict:
    return {"event": "burn", "tender": tender_id, "from": HELDOUT, "to": DEV, "reason": reason}


def append_events(path: Path, events: list[dict], now: datetime | None = None) -> None:
    """Append in one write and fsync, so a crash leaves either all of the events or an unreadable last line (which
    every reader refuses), never a quietly shorter ledger."""
    stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = "".join(json.dumps({"v": 1, "ts": stamp, **event}, sort_keys=True) + "\n" for event in events)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def counts(status: Mapping[str, str]) -> dict[str, int]:
    out = {DEV: 0, HELDOUT: 0, PROSPECTIVE: 0}
    for value in status.values():
        out[value] += 1
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="anbud split", description=__doc__.splitlines()[0])
    parser.add_argument("company_id")
    parser.add_argument("metadata", type=Path, help="CSV with columns: " + ", ".join(FIELDS))
    parser.add_argument("--dry-run", action="store_true", help="show the counts without writing the ledger")
    parser.add_argument("--new-ledger", action="store_true", help="start this company's ledger (first split only)")
    args = parser.parse_args(argv)
    try:
        ledger = paths.ledger_path(args.company_id)
    except ValueError as exc:
        print(f"anbud split: {exc}", file=sys.stderr)
        return 2
    try:
        rows = read_metadata(args.metadata)
        ledger.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (ledger.parent / "ledger.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)  # one split at a time; released when the file closes
            exists = ledger.exists() or ledger.is_symlink()
            if args.new_ledger and exists:
                raise SplitError("this company already has a ledger; leave out --new-ledger")
            if not args.new_ledger and not exists and not args.dry_run:
                raise SplitError("this company has no ledger yet; for its first split, add --new-ledger")
            events = plan_split(load_ledger(ledger), rows)
            if not args.dry_run and events:
                append_events(ledger, events)
            total = counts(load_ledger(ledger).status) if not args.dry_run else None
    except (SplitError, ValueError, OSError, csv.Error) as exc:
        message = str(exc) if isinstance(exc, SplitError) else type(exc).__name__
        print(f"anbud split: {message}", file=sys.stderr)
        return 2
    added = counts({e["tender"]: e["status"] for e in events if e["event"] == "assign"})
    burned = sum(1 for e in events if e["event"] == "burn")
    updated = sum(1 for e in events if e["event"] == "update")
    print(
        f"split{' (dry run)' if args.dry_run else ''}: {added[DEV]} new dev, {added[HELDOUT]} new held-out, "
        f"{added[PROSPECTIVE]} new prospective, {updated} updated, {burned} burned"
    )
    if total is not None:
        print(f"ledger: {total[DEV]} dev, {total[HELDOUT]} held-out, {total[PROSPECTIVE]} prospective")
    return 0


def status_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="anbud split-status")
    parser.add_argument("company_id")
    args = parser.parse_args(argv)
    try:
        ledger = paths.ledger_path(args.company_id)
    except ValueError as exc:
        print(f"anbud split-status: {exc}", file=sys.stderr)
        return 2
    try:
        state = load_ledger(ledger)
    except (SplitError, ValueError, OSError) as exc:
        message = str(exc) if isinstance(exc, SplitError) else type(exc).__name__
        print(f"anbud split-status: {message}", file=sys.stderr)
        return 2
    total = counts(state.status)
    print(f"ledger: {total[DEV]} dev, {total[HELDOUT]} held-out, {total[PROSPECTIVE]} prospective")
    dev = sorted(t for t, s in state.status.items() if s == DEV)
    print("dev tenders: " + (", ".join(dev) if dev else "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
