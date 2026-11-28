"""The hard stop degrades a Granskning run, it never halts it. A lens, merge or verifier call that the hard stop
refuses is skipped: the lens is "not run (budget)" and the review partial, the flag "not verified (budget)", and the
run finishes, exits 0 and is frozen with what it has. Spend never passes the stop. The map and the page cards still
halt, and so does Kalkyl, whose calc a refused call would leave incomplete. A refused call leaves a record under its
request's step key, so a $0 rebuild refuses the same calls and comes out the same. Synthetic tender, scripted fake SDK,
$0.

Each test names the one-line mutation it catches; each was made, seen to fail the test, and reverted."""

from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from granskning_fakes import ScriptedSDK, first_text
from openpyxl import load_workbook
from test_granskning_pipeline import clean_tree, script, tender  # noqa: F401 - fixtures
from test_model_client import FakeMessages, _usage

from anbud import cli, model_client
from anbud.config import load_config
from anbud.evals import freeze
from anbud.granskning.page_cards import SYSTEM as CARDS_SYSTEM
from anbud.granskning.report import PARTIAL_BUDGET, VERIFIED
from anbud.model_client import Budget, BudgetExceeded, ModelClient
from anbud.store import RunStore

SYSTEM = [{"type": "text", "text": "Shared prefix."}]
MESSAGES = [{"role": "user", "content": "Hej"}]
MAX_TOKENS = 4000
# A call that uses all of its output: it costs nearly its reservation, so a call past the stop would show in the spend.
FULL = _usage(input_tokens=1000, output_tokens=MAX_TOKENS)


def _client(fake: FakeMessages, hard_stop: float, degrade: frozenset[str]) -> ModelClient:
    sdk = SimpleNamespace(messages=fake)
    return ModelClient(load_config(), sdk, Budget(hard_stop), run_id="r-1", company_id="co-test", degrade_roles=degrade)


def _call(client: ModelClient, role: str) -> Any:
    return client.call(role=role, step="s", system=SYSTEM, messages=MESSAGES, max_tokens=MAX_TOKENS, effort="low")


def _reservation(client: ModelClient, role: str) -> float:
    return client.config.price_for(client.config.model_for(role)).max_cost(1000, MAX_TOKENS)


def test_a_degrade_role_past_the_stop_is_refused_unsent_never_halts_and_spend_stays_under_the_stop() -> None:
    """Mutation: `reserved = self.budget.reserve(...)` -> `reserved = 0.0 if role in self.degrade_roles else
    self.budget.reserve(...)` (degrade by skipping the reservation): the second call is sent, spend passes the stop."""
    fake = FakeMessages(FULL)
    client = _client(fake, 1.0, frozenset({"verifier"}))
    client.budget.hard_stop = 1.5 * _reservation(client, "verifier")  # room for one call, not two
    _call(client, "verifier")
    for _ in range(2):
        with pytest.raises(BudgetExceeded):
            _call(client, "verifier")
    assert len(fake.sent) == 1 and client.budget.spent <= client.budget.hard_stop
    assert client.budget_refused == 2 and client.halted == 0  # skipped, and the run goes on
    assert client.budget.reserved == pytest.approx(0)


def test_the_map_and_a_client_without_degrade_roles_still_halt_at_the_stop() -> None:
    """Granskning's map (before the lenses, nothing to degrade to) and Kalkyl (no degrade roles: a refused call leaves
    the calc incomplete) still halt. Mutation: `if role in self.degrade_roles:` -> `if self.degrade_roles:` in the
    reservation's except: the map is skipped instead of halting the run."""
    granskning = _client(FakeMessages(FULL), 0.001, frozenset({"lens", "verifier"}))
    with pytest.raises(BudgetExceeded):
        _call(granskning, "map")
    assert granskning.halted == 1 and granskning.budget_refused == 0
    kalkyl = _client(FakeMessages(FULL), 0.001, frozenset())
    with pytest.raises(BudgetExceeded):
        _call(kalkyl, "kalkyl_extract")
    assert kalkyl.halted == 1 and kalkyl.budget_refused == 0


def _paid(monkeypatch: pytest.MonkeyPatch, when: Callable[[dict[str, Any]], bool]) -> tuple[ScriptedSDK, list[Budget]]:
    """The fake SDK, and the run's budgets as they are made. The first time a call that `when` matches is sent, the hard
    stop is pulled in to what is spent and reserved, plus a hair: the call in flight settles, and no later call of its
    size fits. As if the run had been given just that much."""
    budgets: list[Budget] = []

    def make(*args: Any, **kwargs: Any) -> Budget:
        budgets.append(Budget(*args, **kwargs))
        return budgets[-1]

    def scripted(params: dict[str, Any]) -> Any:
        budget = budgets[0]
        if not getattr(budget, "pulled_in", False) and when(params):
            budget.hard_stop = budget.spent + budget.reserved + 0.0001
            budget.pulled_in = True  # type: ignore[attr-defined]
        return script(params)

    sdk = ScriptedSDK(scripted)
    monkeypatch.setattr(model_client, "keychain_key", lambda **_: "sk-test")
    monkeypatch.setattr(model_client, "make_sdk_client", lambda config, key: sdk)
    monkeypatch.setattr(model_client, "Budget", make)
    return sdk, budgets


def _verifier(params: dict[str, Any]) -> bool:
    return params["system"] != CARDS_SYSTEM and "You are the verifier" in first_text(params)


def _lens(params: dict[str, Any]) -> bool:
    return params["system"] != CARDS_SYSTEM and "review lens `" in first_text(params)


def _record(run_id: str) -> dict[str, Any]:
    return json.loads((RunStore("co-001").run_dir(run_id) / "granskning.json").read_text())


def _about(run_dir: Any) -> dict[str, Any]:
    wb = load_workbook(run_dir / "report" / "granskning.xlsx")
    return {r[0].value: r[1].value for r in wb["Om rapporten"].iter_rows()}


def test_under_a_tight_stop_the_verifier_stops_and_the_run_finishes_with_verified_n_of_m(
    tender: Any,  # noqa: F811 - the fixture
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mutation: `raise NotVerified(step)` -> `return None` in the verifier loop: the flags the stop kept from the
    verifier read "Verifieringen gav inget svar", and the report and record don't say verified 1 of 4."""
    sdk, budgets = _paid(monkeypatch, _verifier)
    run = "t-0001-tight-verifier"
    assert cli.main(["run", "co-001", "t-0001", "--run-id", run, "--concurrency", "1"]) == 0
    out = capsys.readouterr().out
    assert sum(map(_verifier, sdk.sent)) == 1  # the rest were never sent
    budget = budgets[0]
    assert budget.spent <= budget.hard_stop

    record = _record(run)["budget"]
    assert record["verifier"] == {"selected": 4, "verified": 1, "not_verified_budget": 3}
    assert record["refused_calls"] == 3 and record["partial"] is False and record["lenses_not_run"] == []
    run_dir = tender / "runs" / run
    items = json.loads((run_dir / "items.json").read_text())
    assert sorted(i["verification"] for i in items if i["verification"] != "not_selected").count("budget") == 3
    page = (run_dir / "report" / "granskning.html").read_text(encoding="utf-8")
    assert f"<strong>{VERIFIED}</strong>: 1 av 4, 3 ej verifierade (budget)" in page
    wb = load_workbook(run_dir / "report" / "granskning.xlsx")
    cells = [c for row in wb["Alla flaggor"].iter_rows(values_only=True) for c in row]
    assert cells.count("Ej verifierad (budget)") == 3  # each flag the stop kept from the verifier says so
    assert _about(run_dir)[VERIFIED] == "1 av 4, 3 ej verifierade (budget)"
    assert "verified 1 of 4" in out and "hard stop reached: 3 calls refused" in out
    assert "frozen: ledger line 1, " in out and freeze.is_frozen("co-001", "t-0001")


def test_under_a_tight_stop_a_lens_that_cannot_reserve_is_not_run_and_the_review_is_partial(
    tender: Any,  # noqa: F811 - the fixture
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mutation: drop "lens" from `DEGRADE_ROLES`: the refused lenses halt the run (exit 5, not frozen) instead."""
    sdk, budgets = _paid(monkeypatch, _lens)
    run = "t-0001-tight-lens"
    assert cli.main(["run", "co-001", "t-0001", "--run-id", run, "--concurrency", "1"]) == 0
    out = capsys.readouterr().out
    budget = budgets[0]
    assert budget.spent <= budget.hard_stop

    sent = {first_text(p).split("review lens `")[1].split("`")[0] for p in sdk.sent if _lens(p)}
    record = _record(run)
    lenses = {lens["lens_id"] for lens in record["lenses"]}
    not_run = set(record["budget"]["lenses_not_run"])
    assert not_run and not_run == lenses - sent  # every lens never sent, and only those
    assert "contract_terms" in sent and record["budget"]["partial"] is True
    assert record["budget"]["refused_calls"] >= len(not_run)
    assert all(lens["error"] == "BudgetExceeded" for lens in record["lenses"] if lens["lens_id"] in not_run)
    page = (tender / "runs" / run / "report" / "granskning.html").read_text(encoding="utf-8")
    assert f"<strong>{PARTIAL_BUDGET}</strong>: {len(lenses) - len(not_run)} av {len(lenses)} linser" in page
    for lens_id in not_run:
        assert (
            f"{lens_id} (inte körd)" in page
            and f"{lens_id} (inte körd)" in _about(tender / "runs" / run)[PARTIAL_BUDGET]
        )
    assert "kostnadsstoppet nåddes under linserna" in page  # the checks gave way to the verifier
    assert "frozen: ledger line 1, " in out and freeze.is_frozen("co-001", "t-0001")


def test_a_degraded_run_rebuilt_at_zero_refuses_the_same_calls_and_comes_out_the_same(
    tender: Any,  # noqa: F811 - the fixture
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mutation: drop the refusal record's `self.store.step_put(...)` in `_refuse`: the rebuild has no record of the
    refused verifier calls, misses the step cache on them, and exits replay-incomplete without being frozen."""
    sdk, _ = _paid(monkeypatch, _verifier)
    run = "t-0001-degraded"
    assert cli.main(["run", "co-001", "t-0001", "--run-id", run, "--concurrency", "1"]) == 0
    capsys.readouterr()
    sent = len(sdk.sent)
    paid = tender / "runs" / run
    names = ("flags.json", "kronor.json", "items.json")
    before = {name: (paid / name).read_text() for name in names}

    rebuild = ["run", "co-001", "t-0001", "--replay", "--base-run", run, "--run-id", f"{run}-r1"]
    assert cli.main([*rebuild, "--concurrency", "3"]) == 0  # any thread timing: each request follows its own record
    out = capsys.readouterr().out
    assert "total $0.00" in out and "frozen: ledger line 2, " in out and len(sdk.sent) == sent
    rebuilt = tender / "runs" / f"{run}-r1"
    assert {name: (rebuilt / name).read_text() for name in names} == before
    assert _record(f"{run}-r1")["budget"] == _record(run)["budget"]
    assert _about(rebuilt)[VERIFIED] == _about(paid)[VERIFIED] == "1 av 4, 3 ej verifierade (budget)"
