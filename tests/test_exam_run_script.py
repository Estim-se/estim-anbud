"""scripts/exam-run.sh: the exam detached from the shell that starts it, its preflight, and its status. A fake `uv`
stands in for the exam; nothing here runs the harness on any tender."""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "exam-run.sh"
FAKE_UV = """#!/bin/sh
# Stands in for `uv run --quiet --frozen anbud eval exam co-002`: two status lines, a pause, an exit code.
echo "anbud eval prospective: 4 tenders"
echo "a line that is not a status line"
sleep "${FAKE_SECONDS:-3}"
echo "paid exam: not complete; its one total over both splits prints when every tender has an attempt"
exit 3
"""


def _fake_path(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(FAKE_UV)
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


def _wait(path: Path, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.exists() and path.read_text().strip():
            return True
        time.sleep(0.1)
    return False


def test_the_exam_outlives_the_shell_that_started_it(tmp_path: Path) -> None:
    """The parent shell, and its whole process group, is killed while the exam runs; the exam still finishes and
    writes its log and exit code. Mutation: dropping `POSIX::setsid();` from launch makes it fail."""
    env = _fake_path(tmp_path)
    run, checkout = tmp_path / "run", tmp_path / "checkout"
    checkout.mkdir()
    parent = subprocess.Popen(
        ["bash", "-c", f'"{SCRIPT}" launch "{run}" "{checkout}" eval exam co-002; sleep 60'],
        env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait(run / "pid", 10), "launch wrote no pid"
        time.sleep(0.5)
        os.killpg(parent.pid, signal.SIGKILL)  # the session stops, the terminal closes, a usage limit hits
        parent.wait(5)
        assert not (run / "exit").exists()  # the exam was still running when its parent died
        assert _wait(run / "exit", 20), "the exam died with its parent"
    finally:
        if parent.poll() is None:
            os.killpg(parent.pid, signal.SIGKILL)
    assert (run / "exit").read_text().strip() == "3"
    assert "paid exam: not complete" in (run / "log").read_text()
    assert (run / "command").read_text().strip() == "anbud eval exam co-002"
    assert stat.S_IMODE(run.stat().st_mode) == 0o700


def test_status_prints_state_counts_and_status_lines_and_of_a_traceback_only_its_type(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "pid").write_text("999999\n")  # no such process
    (run / "exit").write_text("1\n")
    (run / "log").write_text(
        "anbud eval prospective: 4 tenders\n"
        "some other line, kept in the log\n"
        "Traceback (most recent call last):\n"
        '  File "x.py", line 1, in <module>\n'
        "ValueError: a message that may hold a path or a name\n"
        "estimate id: 0123456789ab\n"
    )
    out = subprocess.run([SCRIPT, "status", str(run)], capture_output=True, text=True, check=True).stdout
    assert out.splitlines()[0] == "status: ended; exit 1; log 6 lines, 2 status lines"
    assert "anbud eval prospective: 4 tenders" in out and "estimate id: 0123456789ab" in out
    assert "ending in ValueError (its message stays in the log)" in out
    assert "may hold a path" not in out and "some other line" not in out and "x.py" not in out


def test_start_refuses_when_the_preflight_fails_and_starts_nothing(tmp_path: Path, data_root: Path) -> None:
    """A checkout that isn't a git checkout at the commit fails the preflight; no run folder is made."""
    env = {**_fake_path(tmp_path), "ANBUD_DATA_ROOT": str(data_root), "FAKE_SECONDS": "0"}
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    done = subprocess.run(
        [SCRIPT, "start", "co-002", "0" * 40, "--checkout", str(checkout)], capture_output=True, text=True, env=env
    )
    assert done.returncode == 2 and "preflight failed; nothing started" in done.stderr
    lines = done.stdout.splitlines()
    assert [x.split(" ")[1] for x in lines if x.startswith("preflight: ") and "FAILED" in x][:2] == [
        "checkout",
        "commit",
    ]
    assert lines[-1].startswith("preflight: ") and lines[-1].endswith(" failed")
    assert not (data_root / "predictions" / "co-002" / "exam-runs").exists()


@pytest.mark.parametrize(
    "args",
    [
        ["start", "co-2", "abc"],  # not a company id
        ["start", "co-002", "abc", "--budget", "ten"],  # not a number
        ["status"],
        ["nothing"],
    ],
)
def test_bad_arguments_are_refused(args: list[str]) -> None:
    done = subprocess.run([SCRIPT, *args], capture_output=True, text=True)
    assert done.returncode == 2 and done.stderr.startswith("exam-run: ")
