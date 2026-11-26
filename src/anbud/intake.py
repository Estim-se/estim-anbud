"""`anbud intake`: new tenders from an incoming folder, split before anyone opens a file.

The founder puts each tender in its own folder under `~/estim-anbud-data/incoming/<customer>/`, with the tender
documents in `docs/` (zips are fine) and the bid calculation, if there is one, in `calc/`. Next to the folders,
`metadata.csv` has one row per folder:

    folder, client, property_owner, project_family, prospective

Intake then:
1. reads the CSV only, assigns each new folder the next neutral tender id, and runs the held-out split on the CSV's
   attributes (the same grouping, seed and ledger as `anbud split`);
2. records folder name -> tender id in the local registry, outside every repo;
3. moves held-out and prospective folders, unopened, into `heldout/<company>/<tender>/` with a single rename;
4. moves dev folders into `<company>/dev/<tender>/{inbox,calc}`, unpacks zips there (the zip itself goes to
   `originals/`), and writes the sha256 manifests prep checks.

No tender file is read before the split decides its status, and held-out files are never read at all. The screen gets
counts only: folder names are customer and project names.
"""

from __future__ import annotations

import csv
import fcntl
import hashlib
import os
import shutil
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from anbud import paths
from anbud.evals import split_heldout as sh
from anbud.store import private_dirs

COLUMNS = ("folder", "client", "property_owner", "project_family", "prospective")
MAX_UNZIPPED = 2 * 1024**3
MAX_ENTRIES = 20_000


class IntakeError(RuntimeError):
    """Raised with a reason that names no folder or file."""


@dataclass(frozen=True)
class Row:
    line: int
    folder: str
    client: str
    property_owner: str
    project_family: str
    prospective: bool
    forced_dev: bool


def read_rows(path: Path) -> list[Row]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise IntakeError(f"metadata.csv is missing columns: {', '.join(missing)}")
        rows = []
        for line, raw in enumerate(reader, start=2):
            folder = (raw["folder"] or "").strip()
            if not folder or "/" in folder or folder.startswith("."):
                raise IntakeError(f"metadata.csv line {line}: the folder must be a plain folder name")
            rows.append(
                Row(
                    line,
                    folder,
                    raw["client"] or "",
                    raw["property_owner"] or "",
                    raw["project_family"] or "",
                    sh._parse_bool(raw["prospective"], "prospective", line),
                    sh._parse_bool(raw.get("forced_dev") or "no", "forced_dev", line),
                )
            )
    if len({r.folder for r in rows}) != len(rows):
        raise IntakeError("metadata.csv names a folder twice")
    return rows


# The registry: [companies] co-NNN = "name"; [tenders.co-NNN] t-NNNN = "folder name". Local only.


def load_registry(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + "".join(c if c >= " " else f"\\u{ord(c):04x}" for c in escaped) + '"'


def dump_registry(registry: dict) -> str:
    lines = ["# Neutral id -> real name. Local only; never copy this anywhere.", "", "[companies]"]
    lines += [f"{k} = {_toml_string(v)}" for k, v in sorted(registry.get("companies", {}).items())]
    for company, tenders in sorted(registry.get("tenders", {}).items()):
        lines += ["", f"[tenders.{company}]"]
        lines += [f"{k} = {_toml_string(v)}" for k, v in sorted(tenders.items())]
    return "\n".join(lines) + "\n"


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _next_ids(taken: set[str], count: int) -> list[str]:
    numbers = sorted(int(t[2:]) for t in taken if paths.TENDER_ID.fullmatch(t))
    start = (numbers[-1] if numbers else 0) + 1
    return [f"t-{n:04d}" for n in range(start, start + count)]


def _manifest(folder: Path) -> str:
    lines = []
    for path in sorted(p for p in folder.rglob("*") if p.is_file() and not p.is_symlink()):
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(folder).as_posix()}")
    return "\n".join(lines) + ("\n" if lines else "")


def _safe_unzip(archive: Path, target: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_UNZIPPED:
            raise IntakeError("a zip unpacks to more than the limit")
        for info in infos:
            name = PurePosixPath(info.filename.replace("\\", "/"))
            link = (info.external_attr >> 16) & 0o170000 == 0o120000
            if info.is_dir() or name.is_absolute() or ".." in name.parts or link:
                continue  # directories, escapes and symlinks are not unpacked
            dest = target.joinpath(*name.parts)
            private_dirs(dest.parent)
            with zf.open(info) as src, open(dest, "xb") as out:
                shutil.copyfileobj(src, out)
            os.chmod(dest, 0o600)


def _place_dev(source: Path, tender_dir: Path) -> None:
    docs = source / "docs" if (source / "docs").is_dir() else source
    inbox, calc, originals = tender_dir / "inbox", tender_dir / "calc", tender_dir / "originals"
    private_dirs(tender_dir)
    if (source / "calc").is_dir():
        os.rename(source / "calc", calc)
    if docs is source:
        private_dirs(inbox)
        for entry in sorted(source.iterdir()):
            if entry.name != "calc":
                os.rename(entry, inbox / entry.name)
        source.rmdir()
    else:
        os.rename(docs, inbox)
        leftovers = sorted(source.iterdir())
        if leftovers:
            private_dirs(originals)
            for entry in leftovers:
                os.rename(entry, originals / entry.name)
        source.rmdir()
    for archive in sorted(p for p in inbox.rglob("*") if p.is_file() and zipfile.is_zipfile(p)):
        if archive.suffix.lower() in (".xlsx", ".xlsm", ".docx"):
            continue
        _safe_unzip(archive, archive.with_suffix(""))
        private_dirs(originals)
        os.rename(archive, originals / archive.name)
    _write_private(tender_dir / "inbox.sha256", _manifest(inbox))
    if calc.is_dir():
        _write_private(tender_dir / "calc.sha256", _manifest(calc))


def intake(company_id: str, incoming: Path, root: Path | None = None, new_ledger: bool = False) -> dict[str, int]:
    base = (root or paths.data_root()).expanduser()
    paths.check_company_id(company_id)
    if paths.is_sealed_path(incoming, root):
        raise IntakeError("the incoming folder is inside held-out or sealed storage")
    rows = read_rows(incoming / "metadata.csv")
    present = {p.name for p in incoming.iterdir() if p.is_dir() and not p.name.startswith(".")}
    unlisted = present - {r.folder for r in rows}
    if unlisted:
        raise IntakeError(f"{len(unlisted)} folder(s) have no row in metadata.csv; every tender needs one")
    rows = [r for r in rows if r.folder in present]  # rows whose folder was already moved are skipped
    registry_path = base / "registry.toml"
    ledger = paths.ledger_path(company_id, root)
    private_dirs(ledger.parent)
    with (ledger.parent / "ledger.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        registry = load_registry(registry_path)
        companies = registry.setdefault("companies", {})
        companies.setdefault(company_id, incoming.name)
        tenders = registry.setdefault("tenders", {}).setdefault(company_id, {})
        by_folder = {v: k for k, v in tenders.items()}
        state = sh.load_ledger(ledger) if ledger.exists() else sh.LedgerState()
        if not ledger.exists() and not new_ledger:
            raise IntakeError("this company has no ledger yet; for its first intake, add --new-ledger")
        fresh = [r for r in rows if r.folder not in by_folder]
        for row, tid in zip(fresh, _next_ids(set(tenders) | set(state.status), len(fresh)), strict=True):
            by_folder[row.folder] = tid
        split_rows = []
        for r in rows:
            keys = sh.group_keys(r.client, r.property_owner, r.project_family, r.line)
            if not r.prospective and not sh.normalise(r.client):
                raise IntakeError(f"metadata.csv line {r.line}: client or main contractor is required")
            split_rows.append(sh.TenderRow(by_folder[r.folder], keys, r.prospective, r.forced_dev))
        events = sh.plan_split(state, split_rows)
        if events:
            sh.append_events(ledger, events)
        burned = sum(1 for e in events if e["event"] == "burn")
        for row in rows:
            tenders[by_folder[row.folder]] = row.folder
        _write_private(registry_path, dump_registry(registry))
        status = sh.load_ledger(ledger).status
    moved = {sh.DEV: 0, sh.HELDOUT: 0, sh.PROSPECTIVE: 0}
    for row in rows:
        tid = by_folder[row.folder]
        source = incoming / row.folder
        if status[tid] == sh.DEV:
            target = paths.dev_tender_dir(company_id, tid, root)
            if target.exists():
                raise IntakeError(f"{tid} already has a dev folder; nothing was moved for it")
            _place_dev(source, target)
        else:
            target = base / "heldout" / company_id / tid
            private_dirs(target.parent)
            os.rename(source, target)  # unopened: one rename, nothing read
        moved[status[tid]] += 1
    # A burn moves an earlier held-out tender to dev in the ledger; its folder stays in heldout/ for a person to move.
    return {"dev": moved[sh.DEV], "heldout": moved[sh.HELDOUT], "prospective": moved[sh.PROSPECTIVE], "burned": burned}
