"""`anbud cards <co>`: one case card per dev tender and an aggregate card for the sealed ones, written locally to
`~/estim-anbud-data/notes/<co>/case-cards.md`, a folder both lanes may read.

A dev tender's card holds counts and formats only, read from what intake, prep, the run folders and the freeze ledger
already wrote: how many files of which type, pages, sheets, tiles, scans and drawings, what prep skipped or masked,
what intake held as answer keys, and how many runs exist and are frozen. No file name, no document text and nothing
from an answer key. A held-out or prospective tender is never opened: the card counts them from the split ledger.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from anbud import paths
from anbud.evals import freeze
from anbud.evals import split_heldout as sh
from anbud.store import private_dirs

CARDS_FILE = "case-cards.md"


class CardError(RuntimeError):
    """Raised with a reason that names no file."""


def notes_dir(company_id: str, root: Path | None = None) -> Path:
    return (root or paths.data_root()) / "notes" / paths.check_company_id(company_id)


def _json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _manifest(path: Path) -> list[str]:
    """Relative paths in a `<sha256>  <path>` manifest."""
    if not path.is_file() or path.is_symlink():
        return []
    return [line.split("  ", 1)[1] for line in path.read_text(encoding="utf-8").splitlines() if "  " in line]


def _types(names: list[str]) -> dict[str, int]:
    return dict(Counter(PurePosixPath(n).suffix.casefold().lstrip(".") or "none" for n in names).most_common())


def _size(folder: Path, names: list[str]) -> int:
    total = 0
    for name in names:
        path = folder / name
        if path.is_file() and not path.is_symlink():
            total += path.stat().st_size
    return total


def _runs(folder: Path, smoke: bool = False) -> int:
    """The run folders in `folder`: the smoke runs when `smoke`, otherwise every other run."""
    if not folder.is_dir() or folder.is_symlink():
        return 0
    return sum(
        1
        for p in folder.iterdir()
        if p.is_dir() and not p.is_symlink() and not p.name.startswith(".") and freeze.is_smoke(p.name) == smoke
    )


def dev_card(company_id: str, tender_id: str, frozen: int, root: Path | None = None) -> dict[str, Any]:
    """Counts for one dev tender."""
    tender = paths.dev_tender_dir(company_id, tender_id, root)
    inbox = _manifest(tender / "inbox.sha256")
    calc = _manifest(tender / "calc.sha256")
    summary = _json(tender / "prep" / "summary.json")
    record = _json(tender / "intake.json")
    card: dict[str, Any] = {
        "tender": tender_id,
        "files": {"count": len(inbox), "bytes": _size(tender / "inbox", inbox), "by_type": _types(inbox)},
        "calc_files": {"count": len(calc), "by_type": _types(calc)},
        "prep": {
            k: summary.get(k)
            for k in (
                "documents",
                "documents_by_kind",
                "pages_by_kind",
                "sheets",
                "tiles",
                "skipped",
                "hidden_files_skipped",
                "links_skipped",
                "furniture_dropped",
                "withheld_sections",
                "masked_by_kind",
                "block_chars",
            )
            if k in summary
        },
        "runs": {
            "granskning": _runs(tender / "runs"),
            "kalkyl": _runs(tender / "kalkyl"),
            "frozen": frozen,
            "smoke": _runs(tender / "runs", smoke=True) + _runs(tender / "kalkyl", smoke=True),
        },
    }
    if record:
        card["intake"] = {
            "unpack": record.get("unpack"),
            "held_as_answer_keys": record.get("held"),
            "notice_fields_found": record.get("found"),
        }
    return card


def build(company_id: str, root: Path | None = None) -> dict[str, Any]:
    paths.check_company_id(company_id)
    try:
        status = sh.load_ledger(paths.ledger_path(company_id, root)).status
    except sh.SplitError as exc:
        raise CardError(str(exc)) from None
    try:
        frozen = freeze.freezes_by_tender(freeze.ledger_file(company_id, False, root), root)
    except freeze.FreezeError as exc:
        raise CardError(str(exc)) from None
    counts = Counter(status.values())
    dev = sorted(t for t, s in status.items() if s == sh.DEV)
    return {
        "company": company_id,
        "split": {"dev": counts[sh.DEV], "heldout": counts[sh.HELDOUT], "prospective": counts[sh.PROSPECTIVE]},
        "dev": [dev_card(company_id, t, frozen.get(f"{company_id}/{t}", 0), root) for t in dev],
    }


def _fmt(value: Any) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{k} {_fmt(v)}" for k, v in value.items()) or "none"
    if value is None:
        return "-"
    return str(value)


def render(cards: dict[str, Any], now: datetime | None = None) -> str:
    split = cards["split"]
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    out = [
        f"# Case cards: {cards['company']}",
        "",
        f"Written by `anbud cards` at {stamp}. Counts and formats only: no file names, no document text, nothing from "
        "an answer key.",
        "",
        "## Sealed tenders",
        "",
        f"{split['heldout']} held-out and {split['prospective']} prospective. They are never opened; "
        "`anbud eval heldout` and `anbud eval prospective` report on them in aggregate.",
        "",
        f"## Dev tenders ({split['dev']})",
    ]
    for card in cards["dev"]:
        prep, runs, files = card["prep"], card["runs"], card["files"]
        out += [
            "",
            f"### {card['tender']}",
            "",
            f"- Files: {files['count']} ({files['bytes'] / 1e6:.1f} MB); by type: {_fmt(files['by_type'])}",
            f"- Calculation files: {card['calc_files']['count']}; by type: {_fmt(card['calc_files']['by_type'])}",
            f"- Prep: {_fmt(prep.get('documents'))} documents; by kind: {_fmt(prep.get('documents_by_kind'))}",
            f"- Pages by kind: {_fmt(prep.get('pages_by_kind'))}; sheets {_fmt(prep.get('sheets'))}, "
            f"tiles {_fmt(prep.get('tiles'))}",
            f"- Skipped: {_fmt(prep.get('skipped'))}; hidden files {_fmt(prep.get('hidden_files_skipped'))}, "
            f"links {_fmt(prep.get('links_skipped'))}; furniture dropped {_fmt(prep.get('furniture_dropped'))}; "
            f"web-page sections withheld {_fmt(prep.get('withheld_sections'))}",
            f"- Masked: {_fmt(prep.get('masked_by_kind'))}",
            f"- Runs: {runs['granskning']} review, {runs['kalkyl']} Kalkyl; {runs['frozen']} frozen in the ledger; "
            f"{runs.get('smoke', 0)} smoke runs left out of these counts",
        ]
        if "intake" in card:
            intake = card["intake"]
            out += [
                f"- Intake: unpack {_fmt(intake['unpack'])}",
                f"- Held as answer keys: {_fmt(intake['held_as_answer_keys'])}",
                f"- Notice fields found: {_fmt(intake['notice_fields_found'])}",
            ]
    return "\n".join(out) + "\n"


def write(company_id: str, root: Path | None = None, now: datetime | None = None) -> tuple[Path, dict[str, Any]]:
    cards = build(company_id, root)
    folder = notes_dir(company_id, root)
    if folder.is_symlink() or folder.parent.is_symlink():
        raise CardError("the notes folder is a link; it must be a plain folder")
    private_dirs(folder)
    target = folder / CARDS_FILE
    tmp = target.with_name(f".{CARDS_FILE}.tmp")
    tmp.write_text(render(cards, now), encoding="utf-8")
    tmp.replace(target)
    return target, cards
