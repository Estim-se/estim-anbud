"""`anbud intake`: new tenders from an incoming folder, split before anyone opens a file.

Two kinds of delivery:

- **Public tenders, blind** (no `metadata.csv`): one zip per tender in `~/estim-anbud-data/incoming/<company>/`.
  Nobody fills anything in. Code reads the platform's notice page inside the zip (see `anbud.notice`) for the buyer
  (the split's entity key), the procurement reference (its family key), the bid deadline (still open means
  prospective) and the publication date (counted against the models' training cutoff). The split sees hashed keys
  only. For a dev tender, the buyer's questions and answers, amendments and any award or protocol file are recognised
  by code (file and folder names, pages titled so, and the documents the notice lists under such a heading) and moved
  to `answers/<company>/<tender>/intake/` before prep can see them: they are the answer key.
- **Customer tenders** (with `metadata.csv`), described below.

The founder puts each customer tender in its own folder under `~/estim-anbud-data/incoming/<company>/`, with the tender
documents in `docs/` (zips are fine) and the bid calculation, if there is one, in `calc/`. Next to the folders,
`metadata.csv` has one row per folder:

    folder, client, property_owner, project_family, prospective[, forced_dev]

Intake then:
1. recognises each delivery by a fingerprint, so a delivery seen before keeps its tender id and a new delivery that
   reuses a registered name is refused before anything is written;
2. assigns each new delivery the next neutral tender id and runs the held-out split on the CSV's attributes (the same
   grouping, seed and ledger as `anbud split`);
3. records name, fingerprint and tender id in the local registry, outside every repo;
4. moves held-out and prospective folders, unopened, into `heldout/<company>/<tender>/` with a single rename;
5. builds each dev tender in a staging folder: documents copied to `inbox/` with every `.zip` unpacked (nested zips
   too), the calculation to `calc/`, the sha256 manifests prep checks, and the delivery itself, untouched, under
   `originals/<fingerprint>`. One rename then puts the tender in place. On any error the staging folder is removed and
   the delivery is back where it was, so a rerun simply tries again.

A folder's fingerprint is the identity of the folder itself (one `stat`, no listing), so a held-out folder is never
opened or listed: the same folder, moved back after a failed placement, is the same tender; a new folder under a
registered name is refused. A zip's fingerprint is the hash of its bytes. The screen gets
counts and tender ids only: folder and file names are customer and project names, and no error message carries one.
"""

from __future__ import annotations

import contextlib
import csv
import errno
import fcntl
import hashlib
import io
import os
import re
import shutil
import tempfile
import tomllib
import unicodedata
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path, PurePosixPath

from anbud import notice as notices
from anbud import paths
from anbud.answer_terms import classify_path, fold
from anbud.evals import split_heldout as sh
from anbud.prep.sources import is_ignored
from anbud.store import canonical, private_dirs

COLUMNS = ("folder", "client", "property_owner", "project_family", "prospective")
MAX_UNZIPPED = 2 * 1024**3  # per tender, across every nested zip
MAX_ENTRIES = 20_000  # per tender, across every nested zip
MAX_DEPTH = 5  # zips inside zips
MAX_NAME_BYTES = 200  # one path component, so a long name in a zip can't fail the write
# A deadline that passed this recently may have been moved by an amendment or an answer intake can't read: with such
# files in the delivery, the tender is sealed as prospective rather than opened.
DEADLINE_MOVE_DAYS = 60
# Archives left in a dev inbox (one that couldn't be unpacked, or a kind intake doesn't unpack) are held with the
# answers: their answer keys can't be checked.
ARCHIVE_SUFFIXES = frozenset(".zip .zipx .rar .7z .gz .tgz .tar .bz2 .xz .cab .arj .lzh .lha .z .001".split())


class IntakeError(RuntimeError):
    """Raised with a reason that names no folder or file."""


def nfc(name: str) -> str:
    """The one form a name is compared and stored in: macOS may hand back the decomposed form of 'ö'."""
    return unicodedata.normalize("NFC", name)


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
    if not path.is_file() or path.is_symlink():
        raise IntakeError("the incoming folder has no metadata.csv")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise IntakeError(f"metadata.csv is missing columns: {', '.join(missing)}")
        rows = []
        for line, raw in enumerate(reader, start=2):
            folder = nfc((raw["folder"] or "").strip())
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


# The registry, local only:
#   [companies]        co-NNN = "name"
#   [tenders.co-NNN]   t-NNNN = "name as delivered"
#   [hashes.co-NNN]    t-NNNN = "fingerprint of the delivery"
# Any other table of strings is kept as it is.

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def load_registry(path: Path) -> dict:
    if path.is_symlink():
        raise IntakeError("the registry is a link; it must be a plain file in the data folder")
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _toml_string(value: str) -> str:
    if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise IntakeError("a name isn't valid text; rename it")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + "".join(c if " " <= c != "\x7f" else f"\\u{ord(c):04x}" for c in escaped) + '"'


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else _toml_string(key)


def dump_registry(registry: dict) -> str:
    """Every table of strings, one level of sub-tables. Anything else is refused rather than dropped."""
    lines = ["# Neutral id -> real name. Local only; never copy this anywhere."]
    for table in ["companies", *sorted(k for k in registry if k != "companies")]:
        content = registry.get(table, {})
        if not isinstance(content, dict):
            raise IntakeError("the registry holds a value intake can't write back; fix it by hand")
        strings = {k: v for k, v in content.items() if not isinstance(v, dict)}
        if any(not isinstance(v, str) for v in strings.values()):
            raise IntakeError("the registry holds a value intake can't write back; fix it by hand")
        if strings or table == "companies":
            lines += ["", f"[{_toml_key(table)}]"]
            lines += [f"{_toml_key(k)} = {_toml_string(v)}" for k, v in sorted(strings.items())]
        for sub, values in sorted((k, v) for k, v in content.items() if isinstance(v, dict)):
            if any(not isinstance(v, str) for v in values.values()):
                raise IntakeError("the registry holds a value intake can't write back; fix it by hand")
            lines += ["", f"[{_toml_key(table)}.{_toml_key(sub)}]"]
            lines += [f"{_toml_key(k)} = {_toml_string(v)}" for k, v in sorted(values.items())]
    return "\n".join(lines) + "\n"


def _write_private(path: Path, text: str) -> None:
    """Atomic, private, and with a temp name of its own, so two writers never share one."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    private_dirs(path.parent)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise IntakeError("a lock file is a link; remove it") from None
        raise
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _next_ids(taken: set[str], count: int) -> list[str]:
    numbers = sorted(int(t[2:]) for t in taken if paths.TENDER_ID.fullmatch(t))
    start = (numbers[-1] if numbers else 0) + 1
    return [f"t-{n:04d}" for n in range(start, start + count)]


# Files


def _walk(folder: Path) -> Iterator[tuple[Path, tuple[str, ...]]]:
    """Regular files under a folder with their relative parts, sorted, never through a link, ignored names left out."""

    def unreadable(error: OSError) -> None:
        raise error  # a folder that can't be read would silently drop its files from the inbox

    for dirpath, dirnames, filenames in os.walk(folder, onerror=unreadable, followlinks=False):
        here = Path(dirpath)
        rel = here.relative_to(folder).parts
        dirnames[:] = sorted(d for d in dirnames if not is_ignored((*rel, d)) and not (here / d).is_symlink())
        for name in sorted(filenames):
            path = here / name
            if not is_ignored((*rel, name)) and not path.is_symlink() and path.is_file():
                yield path, (*rel, name)


def _sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _manifest(folder: Path) -> str:
    lines = [f"{_sha256_file(path)}  {'/'.join(parts)}" for path, parts in _walk(folder)]
    return "\n".join(lines) + ("\n" if lines else "")


def folder_fingerprint(folder: Path) -> str:
    """The folder's own identity: device, inode and, where the system keeps one, birth time. One `stat` of the folder,
    nothing inside it listed or opened. A rename keeps it, so a delivery moved back after a failed placement is
    recognised; a copy is a new delivery."""
    st = folder.stat()
    birth = getattr(st, "st_birthtime", None)
    return hashlib.sha256(f"anbud-folder-v2\0{st.st_dev}\0{st.st_ino}\0{birth!r}".encode()).hexdigest()


def _copy_tree(source: Path, target: Path, skip_top: frozenset[str] = frozenset()) -> None:
    """Copy regular files. `skip_top` holds casefolded top-level names to leave out: a disk that ignores case finds
    `Calc/` when asked for `calc/`, so the comparison must ignore case too."""
    for path, parts in _walk(source):
        if parts[0].casefold() in skip_top:
            continue
        dest = target.joinpath(*parts)
        private_dirs(dest.parent)
        shutil.copyfile(path, dest, follow_symlinks=False)
        os.chmod(dest, 0o600)


def _free(path: Path) -> Path:
    """`path`, or `name (2).ext`, `name (3).ext` ... when it is taken (a case-insensitive disk folds 'A' and 'a')."""
    if not path.exists() and not path.is_symlink():
        return path
    for n in range(2, 10_000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise IntakeError("too many files with one name")


def _short(part: str) -> str:
    """A path component cut to MAX_NAME_BYTES, keeping its extension."""
    if len(part.encode()) <= MAX_NAME_BYTES:
        return part
    stem, dot, suffix = part.rpartition(".")
    if not dot or len(suffix) > 16:
        stem, suffix = part, ""
    keep = MAX_NAME_BYTES - len(suffix.encode()) - 1
    cut = stem.encode()[:keep].decode("utf-8", "ignore")
    return f"{cut}.{suffix}" if suffix else cut


# Unpacking


@dataclass
class UnpackStats:
    bytes: int = 0
    entries: int = 0
    archives_unpacked: int = 0
    archives_kept: int = 0  # encrypted, damaged, unsupported, too deep or over the limit: left as delivered
    entries_skipped: int = 0  # links, paths that escape, hidden and system files


class _Keep(Exception):
    """This archive stays in the inbox as delivered."""


def _safe_unzip(archive: Path, target: Path, stats: UnpackStats) -> list[Path]:
    """Unpack into `target` (which must not exist) and return the files written. Raises _Keep, after removing what
    it wrote, when the archive can't be unpacked whole."""
    written: list[Path] = []
    try:
        with zipfile.ZipFile(archive) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir() and not i.filename.endswith("\\")]
            if stats.entries + len(infos) > MAX_ENTRIES or stats.bytes + sum(i.file_size for i in infos) > MAX_UNZIPPED:
                raise _Keep
            if any(i.flag_bits & 0x1 for i in infos):
                raise _Keep  # encrypted
            private_dirs(target)
            skipped = 0
            for info in infos:
                name = PurePosixPath(notices.entry_name(info).replace("\\", "/"))
                parts = tuple(p for p in name.parts if p not in ("", "."))
                link = (info.external_attr >> 16) & 0o170000 == 0o120000
                if not parts or name.is_absolute() or ".." in parts or ":" in parts[0] or link or is_ignored(parts):
                    skipped += 1
                    continue
                dest = _free(target.joinpath(*(_short(p) for p in parts)))
                private_dirs(dest.parent)
                with zf.open(info) as src, open(dest, "xb") as out:
                    shutil.copyfileobj(src, out)
                os.chmod(dest, 0o600)
                written.append(dest)
            stats.entries += len(infos)
            stats.bytes += sum(i.file_size for i in infos)
            stats.entries_skipped += skipped
    except (_Keep, *notices.ZIP_ERRORS):
        # RuntimeError: encrypted entries; NotImplementedError: Deflate64 and other methods zipfile can't read;
        # zlib.error: a damaged stream; OSError: a name the disk refuses. The message may name a file: dropped.
        if target.exists():
            shutil.rmtree(target)
        raise _Keep from None
    return written


def _is_zip(path: Path) -> bool:
    """Only `.zip` files are unpacked: an xlsx, docx, pptx or odt is a zip inside but a document to prep."""
    return path.suffix.casefold() == ".zip" and zipfile.is_zipfile(path)


def unpack_all(inbox: Path, stats: UnpackStats) -> None:
    """Unpack every `.zip` in the inbox into a folder next to it, then the zips that came out, MAX_DEPTH deep. An
    unpacked zip is removed from the inbox: the delivery under `originals/` still holds it."""
    pending = [(p, 1) for p, _ in _walk(inbox) if _is_zip(p)]
    while pending:
        archive, depth = pending.pop(0)
        if depth > MAX_DEPTH:
            stats.archives_kept += 1
            continue
        staging = _free(archive.with_name(f".unpacking-{archive.name}"))
        try:
            written = _safe_unzip(archive, staging, stats)
        except _Keep:
            stats.archives_kept += 1
            continue
        dest = _free(archive.with_name(_short(archive.stem) or "zip"))
        os.rename(staging, dest)
        archive.unlink()
        stats.archives_unpacked += 1
        for path in written:
            inner = dest / path.relative_to(staging)
            if _is_zip(inner):
                pending.append((inner, depth + 1))


# Deliveries


@dataclass
class Delivery:
    name: str  # as delivered, NFC; local only, never printed
    path: Path
    fingerprint: str
    keys: tuple[str, ...] = ()
    prospective: bool = False
    forced_dev: bool = False
    tender_id: str = ""
    notice: notices.Notice | None = None  # zips only
    entity: bool = False  # the notice gave an entity key (org number or buyer)
    sealed_because: str | None = None  # zips only: "open", "no deadline" or "deadline may have moved"

    @property
    def is_zip(self) -> bool:
        return self.notice is not None


@dataclass
class NoticeStats:
    """Counts over the zips taken in this run: safe to print."""

    tenders: int = 0
    without_page: int = 0
    found: dict[str, int] = field(default_factory=dict)
    no_entity_key: int = 0  # neither an org number nor a buyer name: grouped alone, by tender
    no_key: int = 0  # not even a reference: the split's `tender:` key
    before_cutoff: int = 0
    after_cutoff: int = 0
    unknown_date: int = 0
    framework: int = 0
    open: int = 0  # the deadline is still ahead: prospective
    no_deadline: int = 0  # no deadline read: prospective, never dev
    may_have_moved: int = 0  # passed lately, with amendments or answers intake can't read: prospective
    deadline_from_pdf: int = 0  # no notice page gave the deadline; a PDF's first pages did

    def add(
        self, notice: notices.Notice, keys: tuple[str, ...], entity: bool, cutoff: date, sealed_because: str | None
    ) -> None:
        self.tenders += 1
        self.open += sealed_because == "open"
        self.no_deadline += sealed_because == "no deadline"
        self.may_have_moved += sealed_because == "deadline may have moved"
        self.deadline_from_pdf += notice.deadline_from_pdf
        self.without_page += notice.pages == 0
        for name, ok in notice.found().items():
            self.found[name] = self.found.get(name, 0) + ok
        self.no_entity_key += not entity
        self.no_key += not keys
        self.framework += notice.framework
        if notice.published is None:
            self.unknown_date += 1
        elif notice.published < cutoff:
            self.before_cutoff += 1
        else:
            self.after_cutoff += 1


@dataclass
class Result:
    dev: int = 0
    heldout: int = 0
    prospective: int = 0
    burned: int = 0
    already_in: int = 0
    failed: list[str] = field(default_factory=list)  # tender ids, with the error type: "t-0003 BadZipFile"
    unpack: UnpackStats = field(default_factory=UnpackStats)
    notices: NoticeStats = field(default_factory=NoticeStats)
    held: dict[str, int] = field(default_factory=dict)  # files held back as answer keys, by kind (dev tenders)
    tenders_with_held: int = 0
    tenders_mostly_held: int = 0  # more than half of a dev tender's files held: worth a look once it is frozen
    rows_without_folder: int = 0  # metadata.csv rows that name neither a folder here nor a registered tender

    def counts(self) -> dict[str, int]:
        return {"dev": self.dev, "heldout": self.heldout, "prospective": self.prospective, "burned": self.burned}

    def add_unpack(self, stats: UnpackStats) -> None:
        for name in ("bytes", "entries", "archives_unpacked", "archives_kept", "entries_skipped"):
            setattr(self.unpack, name, getattr(self.unpack, name) + getattr(stats, name))


def _placed(base: Path, company_id: str, tender_id: str, root: Path | None) -> bool:
    return (
        paths.dev_tender_dir(company_id, tender_id, root).exists()
        or (base / "heldout" / company_id / tender_id).exists()
    )


def _staging(company_id: str, tender_id: str, root: Path | None) -> Path:
    return paths.company_dir(company_id, root) / "dev" / f".staging-{tender_id}"


def _recover(company_id: str, base: Path, root: Path | None) -> None:
    """Undo placements a crash interrupted: the delivery goes back to the incoming folder under its registered name,
    and the staging folders are removed, so this run places the tender again. Only intake's own `.staging-` folders
    are looked at."""
    dev = paths.company_dir(company_id, root) / "dev"
    heldout = base / "heldout" / company_id
    answers = base / "answers" / company_id
    stagings = [*_glob(dev, ".staging-t-*"), *_glob(heldout, ".staging-t-*"), *_glob(answers, ".staging-t-*")]
    for staging in sorted(stagings):
        _restore(staging, staging.name.removeprefix(".staging-"))
        shutil.rmtree(staging)
    for folder in _glob(answers, "t-*"):  # what intake put in answers/ for a tender that never got placed
        tender_id = folder.name
        if not paths.TENDER_ID.fullmatch(tender_id) or _placed(base, company_id, tender_id, root):
            continue
        _restore(folder, tender_id)
        for name in ("intake", "originals"):
            if (folder / name).is_dir() and not (folder / name).is_symlink():
                shutil.rmtree(folder / name)
        for name in ("intake.sha256", "source.path"):
            (folder / name).unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            folder.rmdir()  # only if nothing else is there


def _restore(folder: Path, tender_id: str) -> None:
    """Put a delivery an interrupted placement left in `folder/originals/` back where `folder/source.path` says it
    came from: the incoming folder this or an earlier run was given."""
    originals = folder / "originals"
    delivered = sorted(originals.iterdir()) if originals.is_dir() and not originals.is_symlink() else []
    if not delivered:
        return
    record = folder / "source.path"
    back = Path(record.read_text(encoding="utf-8")) if record.is_file() else None
    if len(delivered) != 1 or back is None or back.exists() or not back.parent.is_dir():
        raise IntakeError(f"{tender_id} has an interrupted placement that needs a person; nothing was moved")
    os.rename(delivered[0], back)


def _glob(folder: Path, pattern: str) -> list[Path]:
    return sorted(p for p in folder.glob(pattern) if not p.is_symlink()) if folder.is_dir() else []


def _place_dev(delivery: Delivery, company_id: str, root: Path | None, result: Result) -> None:
    stats = UnpackStats()  # per tender: the unpack limits are a tender's, not a run's
    target = paths.dev_tender_dir(company_id, delivery.tender_id, root)
    staging = _staging(company_id, delivery.tender_id, root)
    if target.exists() or staging.exists():
        raise IntakeError(f"{delivery.tender_id} already has a dev folder; nothing was moved for it")
    private_dirs(staging)
    moved = staging / "originals" / delivery.fingerprint
    try:
        source = delivery.path
        folders = {p.name.casefold(): p for p in source.iterdir() if p.is_dir() and not p.is_symlink()}
        docs, calc = folders.get("docs", source), folders.get("calc")
        _copy_tree(docs, staging / "inbox", skip_top=frozenset({"calc"}) if docs is source else frozenset())
        private_dirs(staging / "inbox")
        if calc is not None:
            _copy_tree(calc, staging / "calc")
        unpack_all(staging / "inbox", stats)
        _write_private(staging / "inbox.sha256", _manifest(staging / "inbox"))
        if (staging / "calc").is_dir():
            _write_private(staging / "calc.sha256", _manifest(staging / "calc"))
        private_dirs(moved.parent)
        _write_private(staging / "source.path", str(source))  # where _recover puts it back after a crash
        os.rename(source, moved)  # the commit begins: the delivery leaves the incoming folder...
        try:
            os.rename(staging, target)  # ...and the tender appears, whole, in one rename
        except BaseException:
            os.rename(moved, source)
            raise
    except BaseException:
        if moved.exists() or moved.is_symlink():
            raise  # the delivery is still inside: the next run's _recover puts it back
        if staging.exists():
            shutil.rmtree(staging)
        raise
    result.add_unpack(stats)


def _place_heldout(delivery: Delivery, base: Path, company_id: str) -> None:
    target = base / "heldout" / company_id / delivery.tender_id
    if target.exists():
        raise IntakeError(f"{delivery.tender_id} already has a held-out folder; nothing was moved for it")
    private_dirs(target.parent)
    if not delivery.is_zip:
        os.rename(delivery.path, target)  # unopened: one rename, nothing read
        return
    # A zip: its notice was read, nothing else. It goes in whole, with the notice record, through a staging folder.
    staging = target.with_name(f".staging-{delivery.tender_id}")
    if staging.exists():
        raise IntakeError(f"{delivery.tender_id} has a held-out staging folder; nothing was moved for it")
    moved = staging / "originals" / f"{delivery.fingerprint}.zip"
    private_dirs(moved.parent)
    try:
        _write_private(staging / "intake.json", _intake_record(delivery, None, None))
        _write_private(staging / "source.path", str(delivery.path))
        os.rename(delivery.path, moved)
        try:
            os.rename(staging, target)
        except BaseException:
            os.rename(moved, delivery.path)
            raise
    except BaseException:
        if moved.exists():
            raise
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _intake_record(delivery: Delivery, held: dict[str, int] | None, stats: UnpackStats | None) -> str:
    """The tender's local intake record. Local only, like the files: it names the buyer."""
    record: dict = {"tender": delivery.tender_id, "fingerprint": delivery.fingerprint}
    if delivery.notice is not None:
        notice = delivery.notice.record()
        notice.pop("listed")  # the answer-key documents the notice names stay with the answers
        notice.pop("answer_pages")
        notice.pop("section_pages")
        record["notice"] = notice
        record["found"] = delivery.notice.found()
    if held is not None:
        record["held"] = held
    if stats is not None:
        record["unpack"] = vars(stats)
    return canonical(record).decode() + "\n"


def _hold_answers(inbox: Path, dest: Path, notice: notices.Notice) -> tuple[dict[str, int], list[str]]:
    """Move the answer-key files out of a dev tender's inbox into `dest`, stored by hash. Returns counts by kind and
    the manifest lines (`<sha256>  <kind>  <path in the delivery>`), which stay with the answers.

    Held: files whose name or folder says so, files the notice lists under an answer-key heading, pages titled so
    (unless they carry the notice's labels), and archives intake couldn't unpack, since what is inside them can't be
    checked. A notice page with answer-key sections is held whole and a copy without those sections takes its place.
    """
    counts: dict[str, int] = {}
    lines: list[str] = []
    for path, parts in list(_walk(inbox)):
        suffix = path.suffix.casefold()
        kind: str | None = classify_path(parts)
        replacement: str | None = None
        if kind is None and notices.listed_stem(parts[-1]) in notice.listed:
            kind = "listed"  # the notice lists it under a questions, amendments or award heading
        if kind is None and suffix in ARCHIVE_SUFFIXES:
            kind = "archive"
        if kind is None and suffix in notices.NOTICE_SUFFIXES and path.stat().st_size <= notices.MAX_NOTICE_BYTES:
            page = notices.read_page(path.read_bytes(), path.name)
            if page is not None:
                kind = notices.page_kind(parts, page)
                if kind is None and page[0].answer_lines:
                    kind, replacement = "section", notices.without_answer_sections(page[0])
        if kind is None:
            continue
        sha = _sha256_file(path)
        private_dirs(dest)
        stored = dest / f"{sha}{suffix[:16]}"
        if stored.exists():
            path.unlink()
        else:
            os.rename(path, stored)
        if replacement is not None:
            _write_private(path, replacement)
        counts[kind] = counts.get(kind, 0) + 1
        lines.append(f"{sha}  {kind}  {'/'.join(parts)}")
    return counts, lines


def _place_dev_zip(delivery: Delivery, company_id: str, base: Path, root: Path | None, result: Result) -> None:
    """A public tender split to dev: unpacked into a staging folder, its answer keys held, then committed in three
    renames. The delivery leaves the incoming folder for `answers/<co>/<t>/originals/` (it holds the answer keys, so
    it never sits in the dev folder), the answers appear, and the tender appears last, so it never exists while an
    answer key is anywhere in it. Any error puts the delivery back; a crash leaves it recorded for `_recover`."""
    tender_id = delivery.tender_id
    target = paths.dev_tender_dir(company_id, tender_id, root)
    staging = _staging(company_id, tender_id, root)
    answers = paths.answers_dir(company_id, tender_id, root)
    answers_staging = answers.with_name(f".staging-{tender_id}")
    if target.exists() or staging.exists() or answers.exists() or answers_staging.exists():
        raise IntakeError(f"{tender_id} already has a dev or answers folder; nothing was moved for it")
    original_staged = answers_staging / "originals" / f"{delivery.fingerprint}.zip"
    original_final = answers / "originals" / f"{delivery.fingerprint}.zip"
    stats = UnpackStats()
    where: Path | None = None  # where the delivery is, once it has left the incoming folder
    try:
        private_dirs(staging)
        inbox = staging / "inbox"
        try:
            _safe_unzip(delivery.path, inbox, stats)
            stats.archives_unpacked += 1
            unpack_all(inbox, stats)
        except _Keep:
            stats.archives_kept += 1  # the delivery can't be unpacked: the inbox stays empty, the zip goes to answers
            private_dirs(inbox)
        assert delivery.notice is not None
        delivered = sum(1 for _ in _walk(inbox))
        held, lines = _hold_answers(inbox, answers_staging / "intake", delivery.notice)
        _write_private(staging / "inbox.sha256", _manifest(inbox))
        _write_private(staging / "intake.json", _intake_record(delivery, held, stats))
        private_dirs(original_staged.parent)
        if lines:
            _write_private(answers_staging / "intake.sha256", "\n".join(lines) + "\n")
        _write_private(answers_staging / "source.path", str(delivery.path))  # where _recover puts it back
        private_dirs(answers.parent)
        os.rename(delivery.path, original_staged)
        where = original_staged
        os.rename(answers_staging, answers)
        where = original_final
        os.rename(staging, target)
    except BaseException:
        if where is not None:
            os.rename(where, delivery.path)  # if this fails too, the delivery stays recorded for the next _recover
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(answers_staging, ignore_errors=True)
        if where == original_final:
            shutil.rmtree(answers, ignore_errors=True)
        raise
    result.add_unpack(stats)
    if held:
        result.tenders_with_held += 1
        result.tenders_mostly_held += sum(held.values()) * 2 > delivered
        for kind, n in held.items():
            result.held[kind] = result.held.get(kind, 0) + n


def _check_ledger(ledger: Path, new_ledger: bool) -> None:
    # is_symlink() first: a dangling link isn't there for exists(), and appending would create its target elsewhere.
    if ledger.is_symlink() or ledger.parent.is_symlink():
        raise IntakeError("the split ledger is a link; it must be a plain file in the company folder")
    if new_ledger and ledger.exists():
        raise IntakeError("this company already has a ledger; leave out --new-ledger")
    if not new_ledger and not ledger.exists():
        raise IntakeError("this company has no ledger yet; for its first intake, add --new-ledger")


def _folder_deliveries(incoming: Path, registered: set[str], result: Result) -> list[Delivery]:
    rows = read_rows(incoming / "metadata.csv")
    present = {
        nfc(p.name): p for p in incoming.iterdir() if p.is_dir() and not p.is_symlink() and not is_ignored([p.name])
    }
    unlisted = set(present) - {r.folder for r in rows}
    if unlisted:
        raise IntakeError(f"{len(unlisted)} folder(s) have no row in metadata.csv; every tender needs one")
    deliveries = []
    for r in rows:
        if r.folder not in present:
            result.rows_without_folder += r.folder not in registered  # a typo, or a folder not delivered yet
            continue  # otherwise taken in on an earlier run
        if r.prospective and r.forced_dev:
            raise IntakeError(f"metadata.csv line {r.line}: a tender can't be both prospective and forced dev")
        keys = sh.group_keys(r.client, r.property_owner, r.project_family, r.line)
        if not r.prospective and not sh.normalise(r.client):
            raise IntakeError(f"metadata.csv line {r.line}: client or main contractor is required")
        path = present[r.folder]
        deliveries.append(Delivery(r.folder, path, folder_fingerprint(path), keys, r.prospective, r.forced_dev))
    return deliveries


def notice_keys(notice: notices.Notice) -> tuple[tuple[str, ...], bool]:
    """Hashed split keys from the notice, and whether there is an entity key. The org number and the buyer's name
    are both entity keys, so two notices that share either are one group; the reference is the family key."""
    keys: set[str] = set()
    if notice.org_number:
        keys.add(sh.hash_key("entity:orgnr " + notice.org_number))
    if notice.buyer:
        keys |= set(_quiet_keys(client=notice.buyer))
    entity = bool(keys)
    if notice.reference and len(fold(notice.reference).replace(" ", "")) >= 5:
        keys |= set(_quiet_keys(family=notice.reference))
    return tuple(sorted(keys)), entity


def _quiet_keys(client: str = "", family: str = "") -> tuple[str, ...]:
    try:
        return sh.group_keys(client, "", family)
    except sh.SplitError:
        return ()  # a placeholder-like value: no key from it, counted as missing


def _is_open(deadline: datetime, now: datetime) -> bool:
    if (deadline.hour, deadline.minute) == (0, 0):  # a date alone: open all that day
        return deadline.date() >= now.date()
    return deadline >= now


def _sealed_because(notice: notices.Notice, names: list[tuple[str, ...]], now: datetime) -> str | None:
    """Why a zip must be prospective, or None. Fails closed: a tender is opened only when its deadline was read and
    has passed, and nothing in the delivery could have moved it since."""
    if notice.deadline is None:
        return "no deadline"
    if _is_open(notice.deadline, now):
        return "open"
    if now - notice.deadline < timedelta(days=DEADLINE_MOVE_DAYS):
        kinds = {_member_kind(parts, notice) for parts in names}
        if "award" not in kinds and kinds & {"amendment", "qa", "listed", "page", "section", "archive"}:
            return "deadline may have moved"  # an amendment or answer intake can't read may have extended it
    return None


def _member_kind(parts: tuple[str, ...], notice: notices.Notice) -> str | None:
    """How a member of a delivered zip would be held in a dev inbox, by name and by what the notice says of it."""
    joined = "/".join(parts)
    kind = classify_path(parts)
    if kind is None and notices.listed_stem(parts[-1]) in notice.listed:
        kind = "listed"
    if kind is None and joined in notice.answer_pages:
        kind = "page"
    if kind is None and joined in notice.section_pages:
        kind = "section"
    if kind is None and PurePosixPath(parts[-1]).suffix.casefold() in ARCHIVE_SUFFIXES:
        kind = "archive"
    return kind


def _zip_names(path: Path) -> list[tuple[str, ...]]:
    try:
        with zipfile.ZipFile(path) as zf:
            return list(_member_names(zf, (), 1))
    except notices.ZIP_ERRORS:
        return []


def _zip_deliveries(incoming: Path, now: datetime) -> list[Delivery]:
    entries = [p for p in incoming.iterdir() if not is_ignored([p.name])]
    others = [p for p in entries if p.is_symlink() or not p.is_file() or p.suffix.casefold() != ".zip"]
    if others:
        raise IntakeError(
            f"{len(others)} item(s) in the incoming folder are not zip files; a blind intake takes one zip per tender"
        )
    found = [(path, notices.from_zip(path)) for path in sorted(entries)]
    numbers = {n.org_number for _, n in found}
    if len(found) >= 3 and len(numbers) == 1 and None not in numbers:
        for _, n in found:
            n.org_number = None  # on every notice of a batch: the platform's number, not a buyer's
    deliveries = []
    for path, notice in found:
        keys, entity = notice_keys(notice)
        because = _sealed_because(notice, _zip_names(path), now)
        delivery = Delivery(nfc(path.name), path, _sha256_file(path), keys, because is not None, notice=notice)
        delivery.entity, delivery.sealed_because = entity, because
        deliveries.append(delivery)
    return deliveries


def _member_names(zf: zipfile.ZipFile, prefix: tuple[str, ...], depth: int) -> Iterator[tuple[str, ...]]:
    """Member paths as intake unpacks them, nested zips opened as folders. A zip that can't be opened is a member."""
    for info in zf.infolist():
        if info.is_dir() or info.filename.endswith("\\"):
            continue
        parts = notices.member_parts(info, prefix)
        if len(parts) == len(prefix) or is_ignored(parts):
            continue
        if PurePosixPath(parts[-1]).suffix.casefold() == ".zip" and depth < MAX_DEPTH:
            try:
                if info.file_size <= notices.MAX_INNER_ZIP_BYTES and not info.flag_bits & 0x1:
                    with zipfile.ZipFile(io.BytesIO(zf.read(info))) as inner:
                        yield from _member_names(inner, notices.nested_prefix(parts), depth + 1)
                        continue
            except notices.ZIP_ERRORS:
                pass
        yield parts


def survey(incoming: Path, now: datetime | None = None, root: Path | None = None) -> tuple[Result, int]:
    """`anbud intake --dry-run` for zips: what the notices give, and how many files would be held as answer keys,
    over every zip. Nothing is written and nothing is split, so no tender's status is known or shown. Also returns
    how many deadlines are still open."""
    incoming = incoming.expanduser()
    if incoming.is_symlink() or not incoming.is_dir() or (incoming / "metadata.csv").exists():
        raise IntakeError("a dry run takes an incoming folder of zips, with no metadata.csv")
    if paths.is_sealed_path(incoming, root):
        raise IntakeError("the incoming folder is inside held-out or sealed storage")
    result = Result()
    cutoff = training_cutoff()
    deliveries = _zip_deliveries(incoming, now or datetime.now())
    for d in deliveries:
        assert d.notice is not None
        result.notices.add(d.notice, d.keys, d.entity, cutoff, d.sealed_because)
        held: dict[str, int] = {}
        for parts in _zip_names(d.path):
            if kind := _member_kind(parts, d.notice):
                held[kind] = held.get(kind, 0) + 1
        if held:
            result.tenders_with_held += 1
            for kind, n in held.items():
                result.held[kind] = result.held.get(kind, 0) + n
    return result, result.notices.open


def training_cutoff() -> date:
    config = tomllib.loads((paths.repo_root() / "config" / "intake.toml").read_text(encoding="utf-8"))
    return date.fromisoformat(config["contamination"]["training_cutoff"])


def intake(
    company_id: str,
    incoming: Path,
    root: Path | None = None,
    new_ledger: bool = False,
    company_name: str | None = None,
    now: datetime | None = None,
) -> Result:
    base = (root or paths.data_root()).expanduser()
    try:
        paths.check_company_id(company_id)
    except ValueError as exc:
        raise IntakeError(str(exc)) from None
    incoming = incoming.expanduser()
    if incoming.is_symlink() or not incoming.is_dir():
        raise IntakeError("the incoming folder doesn't exist or is a link")
    if paths.is_sealed_path(incoming, root):
        raise IntakeError("the incoming folder is inside held-out or sealed storage")
    registry_path = base / "registry.toml"
    ledger = paths.ledger_path(company_id, root)
    result = Result()
    # One lock for the registry, which every company shares, then the company's ledger lock (the order `anbud split`
    # can't break: it takes the ledger lock only). Both are held until every tender is placed.
    with _locked(base / "registry.lock"):
        if ledger.parent.is_symlink():
            raise IntakeError("the split folder is a link; it must be a plain folder in the company folder")
        private_dirs(ledger.parent)
        with _locked(ledger.parent / "ledger.lock"):
            _check_ledger(ledger, new_ledger)  # under the lock, so `anbud split --new-ledger` can't race it
            registry = load_registry(registry_path)
            tenders = registry.setdefault("tenders", {}).setdefault(company_id, {})
            hashes = registry.setdefault("hashes", {}).setdefault(company_id, {})
            _recover(company_id, base, root)
            blind = not (incoming / "metadata.csv").exists()
            if blind:
                deliveries = _zip_deliveries(incoming, now or datetime.now())
            else:
                deliveries = _folder_deliveries(incoming, {nfc(v) for v in tenders.values()}, result)
            todo = _identify(deliveries, tenders, hashes, base, company_id, root, result)
            state = sh.load_ledger(ledger)
            cutoff = training_cutoff() if blind else None
            for d in todo:
                if d.notice is not None and cutoff is not None:
                    # Prospective is decided at the first split; a deadline that has passed since changes nothing.
                    if d.tender_id in state.status:
                        d.prospective = state.is_prospective(d.tender_id)
                    result.notices.add(d.notice, d.keys, d.entity, cutoff, d.sealed_because)
            fresh = [d for d in todo if not d.tender_id]
            for delivery, tender_id in zip(fresh, _next_ids(set(tenders) | set(state.status), len(fresh)), strict=True):
                delivery.tender_id = tender_id
            rows = [sh.TenderRow(d.tender_id, d.keys, d.prospective, d.forced_dev) for d in todo]
            events = sh.plan_split(state, rows)
            result.burned = sum(1 for e in events if e["event"] == "burn")
            # The registry first: if the append below never happens, the next run finds these ids by fingerprint
            # and assigns them then. The other order would leave ledger ids that no delivery maps to.
            registry.setdefault("companies", {}).setdefault(company_id, company_name or company_id)
            for d in todo:
                tenders[d.tender_id] = d.name
                hashes[d.tender_id] = d.fingerprint
            _write_private(registry_path, dump_registry(registry))
            if events:
                sh.append_events(ledger, events)
            status = sh.load_ledger(ledger).status
            for d in todo:
                try:
                    if status[d.tender_id] == sh.DEV:
                        if d.is_zip:
                            _place_dev_zip(d, company_id, base, root, result)
                        else:
                            _place_dev(d, company_id, root, result)
                        result.dev += 1
                    else:
                        _place_heldout(d, base, company_id)
                        if status[d.tender_id] == sh.HELDOUT:
                            result.heldout += 1
                        else:
                            result.prospective += 1
                except Exception as exc:  # noqa: BLE001 - other messages may name a file; the type and id are enough
                    reason = str(exc) if isinstance(exc, IntakeError) else type(exc).__name__
                    result.failed.append(f"{d.tender_id}: {reason.removeprefix(d.tender_id + ' ')}")
    # A burn moves an earlier held-out tender to dev in the ledger; its folder stays in heldout/ for a person to move.
    return result


def _identify(
    deliveries: list[Delivery],
    tenders: dict[str, str],
    hashes: dict[str, str],
    base: Path,
    company_id: str,
    root: Path | None,
    result: Result,
) -> list[Delivery]:
    """Match each delivery to a registered tender by fingerprint. A new delivery under a registered name is refused
    before anything is written: treating it as the old tender would log an update that could burn held-out tenders."""
    by_hash = {v: k for k, v in hashes.items()}
    by_name = {nfc(v): k for k, v in tenders.items()}
    if len({d.fingerprint for d in deliveries}) != len(deliveries):
        raise IntakeError("two deliveries in the incoming folder are identical; remove one")
    clashes = sum(1 for d in deliveries if d.fingerprint not in by_hash and d.name in by_name)
    if clashes:
        raise IntakeError(
            f"{clashes} new deliver{'y has' if clashes == 1 else 'ies have'} the name of a registered tender; "
            "rename and run again. Nothing was written"
        )
    todo = []
    for d in deliveries:
        if d.fingerprint in by_hash:
            d.tender_id = by_hash[d.fingerprint]
            if _placed(base, company_id, d.tender_id, root):
                result.already_in += 1  # the same delivery again; left where it is
                continue
        todo.append(d)
    return todo
