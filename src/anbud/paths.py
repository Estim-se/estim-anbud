"""Where the harness keeps data: outside every repo, namespaced by neutral company and tender ids."""

from __future__ import annotations

import os
import re
from pathlib import Path

COMPANY_ID = re.compile(r"^co-\d{3}$")
TENDER_ID = re.compile(r"^t-\d{4}$")


class RealDataInTest(RuntimeError):
    """A test reached for the real data folder."""


def data_root() -> Path:
    override = os.environ.get("ANBUD_DATA_ROOT")
    root = Path(override).expanduser() if override else Path.home() / "estim-anbud-data"
    if os.environ.get("PYTEST_CURRENT_TEST") and _is_real_root(root):
        raise RealDataInTest("a test may never use the real data folder; set ANBUD_DATA_ROOT to a temp folder")
    return root


def _is_real_root(root: Path) -> bool:
    real = Path.home() / "estim-anbud-data"
    try:
        return root.resolve() == real.resolve() or real.resolve() in root.resolve().parents
    except OSError:
        return True  # can't tell: refuse


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def names_file() -> Path:
    override = os.environ.get("ANBUD_NAMES_FILE")
    return Path(override).expanduser() if override else data_root() / "names.txt"


def check_company_id(company_id: str) -> str:
    if not COMPANY_ID.fullmatch(company_id):
        raise ValueError("company ids look like co-001")
    return company_id


def check_tender_id(tender_id: str) -> str:
    if not TENDER_ID.fullmatch(tender_id):
        raise ValueError("tender ids look like t-0001")
    return tender_id


def company_dir(company_id: str, root: Path | None = None) -> Path:
    return (root or data_root()) / check_company_id(company_id)


def dev_tender_dir(company_id: str, tender_id: str, root: Path | None = None) -> Path:
    return company_dir(company_id, root) / "dev" / check_tender_id(tender_id)


def fixture_data_dir(company_id: str, fixture: Path, root: Path | None = None) -> Path:
    """Where a synthetic fixture's prep, runs and labels go: named by its path under fixtures/synthetic/, with `-` for
    `/`, so `injection-tender/injected` and `injection-tender/clean` don't share a folder."""
    base = (repo_root() / "fixtures" / "synthetic").resolve()
    rel = fixture.expanduser().resolve().relative_to(base)
    return company_dir(company_id, root) / "fixtures" / "-".join(rel.parts)


def ledger_path(company_id: str, root: Path | None = None) -> Path:
    return company_dir(company_id, root) / "split" / "ledger.jsonl"


def predictions_dir(company_id: str, root: Path | None = None) -> Path:
    return (root or data_root()) / "predictions" / check_company_id(company_id)


def baseline_dir(company_id: str, tender_id: str, root: Path | None = None) -> Path:
    """Old-engine baseline runs, `predictions/<co>/baseline/<t>/`: eval lane and orchestrator only, never `dev/`, so
    the build lane never sees the old engine's totals."""
    return predictions_dir(company_id, root) / "baseline" / check_tender_id(tender_id)


def answers_dir(company_id: str, tender_id: str, root: Path | None = None) -> Path:
    return (root or data_root()) / "answers" / check_company_id(company_id) / check_tender_id(tender_id)


def sealed_roots(root: Path | None = None) -> tuple[Path, Path]:
    base = (root or data_root()).expanduser().resolve()
    return (base / "heldout").resolve(), (base / "sealed").resolve()


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_dev, st.st_ino


def is_sealed_path(path: Path, root: Path | None = None) -> bool:
    """True if the path, after resolving symlinks, is held-out or sealed storage or inside it.

    Compared by path and by file identity, so a symlinked root or a case variant ("HELDOUT" on macOS) counts too.
    """
    resolved = path.expanduser().resolve()
    roots = sealed_roots(root)
    if any(resolved == s or s in resolved.parents for s in roots):
        return True
    sealed_ids = {i for i in map(_identity, roots) if i is not None}
    return any(_identity(p) in sealed_ids for p in (resolved, *resolved.parents)) if sealed_ids else False
