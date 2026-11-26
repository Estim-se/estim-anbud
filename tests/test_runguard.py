from __future__ import annotations

from pathlib import Path

import pytest

from anbud import cli, paths
from anbud.evals import split_heldout as sh
from anbud.runguard import RunRefused, check_run_allowed, held_out_refusal, in_agent_session


def ledger_with(data_root: Path, **status: str) -> None:
    events = [sh._assign(t.replace("_", "-"), s, "test", ()) for t, s in status.items()]
    sh.append_events(paths.ledger_path("co-001"), events)


def test_dev_tender_is_allowed(data_root: Path) -> None:
    ledger_with(data_root, t_0001=sh.DEV)
    assert check_run_allowed("co-001", "t-0001") == "dev"


@pytest.mark.parametrize("status", [sh.HELDOUT, sh.PROSPECTIVE])
def test_held_out_and_prospective_are_refused(data_root: Path, status: str) -> None:
    ledger_with(data_root, t_0002=status)
    with pytest.raises(RunRefused):
        check_run_allowed("co-001", "t-0002")


def test_unknown_tender_is_refused(data_root: Path) -> None:
    with pytest.raises(RunRefused):
        check_run_allowed("co-001", "t-0009")


def test_non_neutral_ids_are_refused(data_root: Path) -> None:
    with pytest.raises(RunRefused):
        check_run_allowed("co-001", "Projektnamn")
    with pytest.raises(RunRefused):
        check_run_allowed("Kundnamn", "t-0001")


def test_a_dev_path_that_resolves_into_sealed_storage_is_refused(data_root: Path) -> None:
    ledger_with(data_root, t_0003=sh.DEV)
    sealed = data_root / "heldout" / "co-001" / "t-0003"
    sealed.mkdir(parents=True)
    dev_parent = data_root / "co-001" / "dev"
    dev_parent.mkdir(parents=True)
    (dev_parent / "t-0003").symlink_to(sealed)
    with pytest.raises(RunRefused):
        check_run_allowed("co-001", "t-0003")


def test_fixtures_only_from_fixtures_synthetic(tmp_path: Path) -> None:
    assert check_run_allowed("co-001", None, paths.repo_root() / "fixtures" / "synthetic") == "fixture"
    with pytest.raises(RunRefused):
        check_run_allowed("co-001", None, tmp_path)


def test_cli_run_exit_codes(data_root: Path) -> None:
    ledger_with(data_root, t_0001=sh.DEV, t_0002=sh.HELDOUT)
    assert cli.main(["run", "co-001", "t-0002"]) == cli.REFUSED
    assert cli.main(["run", "co-001", "t-0001"]) == cli.NOT_BUILT


def test_held_out_eval_is_refused_in_an_agent_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    assert cli.main(["eval", "heldout"]) == cli.REFUSED
    assert cli.main(["eval", "prospective"]) == cli.REFUSED
    assert in_agent_session({"CLAUDECODE": "1"})
    assert not in_agent_session({})


def test_a_symlinked_held_out_root_still_counts_as_sealed(data_root: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "co-001" / "t-0004").mkdir(parents=True)
    (data_root / "heldout").symlink_to(elsewhere)
    ledger_with(data_root, t_0004=sh.DEV)
    dev_parent = data_root / "co-001" / "dev"
    dev_parent.mkdir(parents=True)
    (dev_parent / "t-0004").symlink_to(elsewhere / "co-001" / "t-0004")
    with pytest.raises(RunRefused):
        check_run_allowed("co-001", "t-0004")


def test_a_case_variant_of_held_out_counts_on_a_case_insensitive_disk(data_root: Path) -> None:
    (data_root / "heldout" / "x").mkdir(parents=True)
    if not (data_root / "HELDOUT").exists():
        pytest.skip("case-sensitive file system")
    assert paths.is_sealed_path(data_root / "HELDOUT" / "x")


def test_a_corrupt_ledger_refuses_instead_of_crashing(data_root: Path) -> None:
    ledger = paths.ledger_path("co-001")
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"event": "assign", "tender": "t-0001", "sta', encoding="utf-8")
    with pytest.raises(RunRefused):
        check_run_allowed("co-001", "t-0001")


class _Stream:
    def __init__(self, tty: bool) -> None:
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def test_held_out_eval_needs_a_person_at_a_terminal() -> None:
    tty, pipe = _Stream(True), _Stream(False)
    assert held_out_refusal({}, tty, tty) is None
    assert held_out_refusal({}, pipe, tty)
    assert held_out_refusal({}, tty, pipe)
    for env in ({"CODEX_SANDBOX": "seatbelt"}, {"AI_AGENT": "1"}, {"CLAUDE_CODE_ENTRYPOINT": "cli"}):
        assert in_agent_session(env)
        assert held_out_refusal(env, tty, tty)


def test_a_symlink_loop_refuses_instead_of_crashing(data_root: Path) -> None:
    ledger_with(data_root, t_0005=sh.DEV)
    dev_parent = data_root / "co-001" / "dev"
    dev_parent.mkdir(parents=True)
    (dev_parent / "t-0005").symlink_to(dev_parent / "t-0005")
    with pytest.raises(RunRefused):
        check_run_allowed("co-001", "t-0005")
