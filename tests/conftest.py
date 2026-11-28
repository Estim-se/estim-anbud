"""Shared test fixtures. Every name here is invented; nothing real is used to test the guards."""

from __future__ import annotations

from pathlib import Path

import pytest

FAKE_NAMES = """\
# invented names for tests only
Fiktivbolaget AB
Påhittade Fastigheter
=Ampere
Örjan Exempelsson
555-123 45 67
"""

# Every surface form a test might print. Output must never contain any of them.
FAKE_SURFACES = [
    "Fiktivbolaget",
    "fiktivbolaget",
    "Påhittade",
    "Pahittade",
    "Fastigheter",
    "Ampere",
    "Örjan",
    "Orjan",
    "Exempelsson",
    "555-123",
]


@pytest.fixture
def names_file(tmp_path: Path) -> Path:
    path = tmp_path / "names.txt"
    path.write_text(FAKE_NAMES, encoding="utf-8")
    return path


@pytest.fixture
def local_env(names_file: Path) -> dict[str, str]:
    """An environment like the Mac's: no CI, no hashed secret, the plain list in a temp file."""
    return {"ANBUD_NAMES_FILE": str(names_file)}


@pytest.fixture(autouse=True)
def _never_the_real_data_root(tmp_path_factory: pytest.TempPathFactory) -> object:
    """No test reaches ~/estim-anbud-data, not even one whose monkeypatch.undo() also undoes `data_root`: that
    leaves this fallback in place, because it is set on a MonkeyPatch of its own."""
    patch = pytest.MonkeyPatch()
    patch.setenv("ANBUD_DATA_ROOT", str(tmp_path_factory.mktemp("not-the-real-data")))
    patch.delenv("ANBUD_EVAL_LANE", raising=False)  # the eval lane's own sessions set it; no test inherits the gate
    yield
    patch.undo()


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient error is still tried again, as many times as in a real run, but without the wait."""
    from anbud import model_client

    monkeypatch.setattr(model_client, "RETRY_DELAYS", tuple(0.0 for _ in model_client.RETRY_DELAYS))


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("ANBUD_DATA_ROOT", str(root))
    return root


def assert_no_fake_names(*texts: str) -> None:
    for text in texts:
        for surface in FAKE_SURFACES:
            assert surface not in text, "a fake name leaked into output"
