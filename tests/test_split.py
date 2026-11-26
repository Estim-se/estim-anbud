"""The held-out split, with invented organisations only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import assert_no_fake_names

from anbud import paths
from anbud.evals import split_heldout as sh

HEADER = "tender_id,client,property_owner,project_family,prospective,forced_dev\n"


def row(tid: str, client: str = "", owner: str = "", family: str = "", *, prospective=False, forced=False):
    return sh.TenderRow(tid, sh.group_keys(client, owner, family), prospective, forced)


def statuses(events: list[dict]) -> dict[str, str]:
    return {e["tender"]: e["status"] for e in events if e["event"] == "assign"}


def test_tenders_sharing_any_attribute_land_on_the_same_side() -> None:
    events = sh.plan_split(
        sh.LedgerState(),
        [
            row("t-0001", client="Fiktiva Bygg AB", owner="Exempelhem"),
            row("t-0002", client="Annat Bygg", owner="Exempelhem"),  # shares the owner with t-0001
            row("t-0003", client="Annat Bygg", family="Stambyte norr"),  # shares the client with t-0002
        ],
    )
    assert len(set(statuses(events).values())) == 1


def test_client_in_one_tender_and_owner_in_another_is_one_group() -> None:
    events = sh.plan_split(sh.LedgerState(), [row("t-0001", client="Exempelhem AB"), row("t-0002", owner="exempelhem")])
    assert len(set(statuses(events).values())) == 1


def test_forced_dev_pulls_its_whole_group_into_dev() -> None:
    events = sh.plan_split(
        sh.LedgerState(),
        [
            row("t-0001", owner="Exempelhem", forced=True),
            row("t-0002", owner="Exempelhem"),
            row("t-0003", owner="Annan ägare"),
        ],
    )
    got = statuses(events)
    assert got["t-0001"] == got["t-0002"] == sh.DEV


def test_a_bridge_burns_the_held_out_side() -> None:
    state = sh.LedgerState(
        status={"t-0001": sh.HELDOUT, "t-0002": sh.DEV},
        keys={"t-0001": sh.group_keys("Kund A", "", ""), "t-0002": sh.group_keys("Kund B", "", "")},
    )
    events = sh.plan_split(state, [row("t-0003", client="Kund A", owner="Kund B")])
    assert {"event": "burn", "tender": "t-0001", "from": sh.HELDOUT, "to": sh.DEV, "reason": "bridge"} in events
    assert statuses(events) == {"t-0003": sh.DEV}


def test_joining_a_held_out_group_stays_held_out() -> None:
    state = sh.LedgerState(status={"t-0001": sh.HELDOUT}, keys={"t-0001": sh.group_keys("Kund A", "", "")})
    assert statuses(sh.plan_split(state, [row("t-0002", owner="Kund A")])) == {"t-0002": sh.HELDOUT}


def test_prospective_tenders_get_their_own_status_and_move_nobody() -> None:
    state = sh.LedgerState(status={"t-0001": sh.HELDOUT}, keys={"t-0001": sh.group_keys("Kund A", "", "")})
    events = sh.plan_split(state, [row("t-0002", client="Kund A", prospective=True)])
    assert events == [
        sh._assign("t-0002", sh.PROSPECTIVE, "prospective", sh.group_keys("Kund A", "", ""), prospective=True)
    ]


def test_the_same_seed_gives_the_same_split() -> None:
    rows = [row(f"t-{i:04d}", client=f"Kund {i}") for i in range(1, 40)]
    assert sh.plan_split(sh.LedgerState(), rows) == sh.plan_split(sh.LedgerState(), rows)


def test_the_dev_share_is_near_sixty_percent_over_many_groups() -> None:
    rows = [row(f"t-{i:04d}", client=f"Kund nummer {i}") for i in range(1, 401)]
    got = statuses(sh.plan_split(sh.LedgerState(), rows))
    share = sum(1 for s in got.values() if s == sh.DEV) / len(got)
    assert 0.5 < share < 0.7


def test_assignments_are_never_recomputed(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    rows = [row("t-0001", client="Kund A")]
    sh.append_events(ledger, sh.plan_split(sh.load_ledger(ledger), rows))
    status = sh.load_ledger(ledger).status["t-0001"]
    assert sh.plan_split(sh.load_ledger(ledger), rows) == []
    # Corrected keys are logged as an update; the status stays.
    events = sh.plan_split(sh.load_ledger(ledger), [row("t-0001", client="Kund B")])
    assert [e["event"] for e in events] == ["update"]
    sh.append_events(ledger, events)
    state = sh.load_ledger(ledger)
    assert state.status["t-0001"] == status
    assert sh.plan_split(state, [row("t-0001", client="Kund B")]) == []


def test_ids_must_be_neutral(tmp_path: Path) -> None:
    meta = tmp_path / "meta.csv"
    meta.write_text(HEADER + "Projektnamn,Kund,,,no,no\n", encoding="utf-8")
    with pytest.raises(sh.SplitError):
        sh.read_metadata(meta)


def test_cli_prints_counts_only_and_the_ledger_holds_no_names(
    data_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    meta = tmp_path / "meta.csv"
    meta.write_text(
        HEADER
        + "t-0001,Fiktivbolaget AB,Påhittade Fastigheter,Stambyte Örjan,no,yes\n"
        + "t-0002,Annan Kund,Ampere Bostäder,,no,no\n"
        + "t-0003,Tredje Kund,,,yes,no\n",
        encoding="utf-8",
    )
    assert sh.main(["co-001", str(meta)]) == 2  # no ledger yet: the first split must say so
    assert sh.main(["co-001", str(meta), "--new-ledger"]) == 0
    assert sh.main(["co-001", str(meta), "--new-ledger"]) == 2  # and only once
    out, err = capsys.readouterr()
    assert "t-000" not in out
    ledger_text = paths.ledger_path("co-001").read_text(encoding="utf-8")
    assert_no_fake_names(out, err, ledger_text)
    assert sh.status_of("co-001", "t-0001") == sh.DEV
    assert sh.status_of("co-001", "t-0003") == sh.PROSPECTIVE


def held_out_and_dev() -> sh.LedgerState:
    return sh.LedgerState(
        status={"t-0001": sh.HELDOUT, "t-0002": sh.DEV},
        keys={"t-0001": sh.group_keys("Kund A", "", ""), "t-0002": sh.group_keys("Kund B", "", "")},
    )


def test_a_correction_that_joins_a_dev_group_burns_the_held_out_side() -> None:
    events = sh.plan_split(
        held_out_and_dev(), [row("t-0001", client="Kund A", owner="Kund B"), row("t-0002", "Kund B")]
    )
    assert [e["event"] for e in events] == ["burn", "update"]
    assert events[0]["tender"] == "t-0001" and events[0]["reason"] == "update"


def test_a_correction_inside_a_held_out_group_moves_nothing() -> None:
    events = sh.plan_split(held_out_and_dev(), [row("t-0001", client="Kund A", family="Etapp 2")])
    assert [e["event"] for e in events] == ["update"]


def test_forced_dev_switched_on_later_burns_the_tender() -> None:
    events = sh.plan_split(held_out_and_dev(), [row("t-0001", client="Kund A", forced=True)])
    assert [(e["event"], e.get("reason")) for e in events] == [("burn", "forced_dev"), ("update", None)]


def test_forced_dev_switched_off_or_prospective_changed_is_refused() -> None:
    state = sh.LedgerState()
    events = sh.plan_split(state, [row("t-0001", client="Kund A", forced=True), row("t-0002", client="Kund B")])
    for event in events:
        if event["event"] == "assign":
            state.status[event["tender"]] = event["status"]
            state.keys[event["tender"]] = tuple(event["keys"])
            state.forced_dev[event["tender"]] = event["forced_dev"]
            state.prospective[event["tender"]] = event["prospective"]
    with pytest.raises(sh.SplitError):
        sh.plan_split(state, [row("t-0001", client="Kund A")])
    with pytest.raises(sh.SplitError):
        sh.plan_split(state, [row("t-0002", client="Kund B", prospective=True)])
    with pytest.raises(sh.SplitError):
        sh.plan_split(sh.LedgerState(), [row("t-0003", client="Kund C", prospective=True, forced=True)])


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Fiktiva Bygg AB", "fiktiva bygg"),
        ("Fiktiva Bygg A.B.", "Fiktiva Bygg"),
        ("Aktiebolaget Fiktiva Bygg", "Fiktiva Bygg"),
        ("Exempelgården ek. för.", "Exempelgarden"),
        ("Exempelgården Ekonomisk Förening", "exempelgarden"),
        ("Exempel Handelsbolag", "Exempel HB"),
    ],
)
def test_spellings_of_one_organisation_normalise_alike(a: str, b: str) -> None:
    assert sh.normalise(a) == sh.normalise(b)


def write_meta(tmp_path: Path, *rows: str) -> Path:
    meta = tmp_path / "meta.csv"
    meta.write_text(HEADER + "".join(r + "\n" for r in rows), encoding="utf-8")
    return meta


@pytest.mark.parametrize("value", ["N/A", "n.a.", "Okänd", "saknas", "-?-unknown", "0", "Privatperson"])
def test_placeholders_are_refused_without_echoing_them(tmp_path: Path, value: str) -> None:
    meta = write_meta(tmp_path, "t-0001,Kund A,,,no,no", f"t-0002,{value},,,no,no")
    with pytest.raises(sh.SplitError) as info:
        sh.read_metadata(meta)
    assert "line 3" in str(info.value) and value not in str(info.value)


def test_a_blank_client_is_refused_and_nothing_is_written(data_root: Path, tmp_path: Path) -> None:
    meta = write_meta(tmp_path, "t-0001,,Påhittade Fastigheter,,no,yes")
    assert sh.main(["co-001", str(meta), "--new-ledger"]) == 2
    assert not paths.ledger_path("co-001").exists()
    # A prospective tender may be incomplete: it never joins a group.
    assert sh.read_metadata(write_meta(tmp_path, "t-0002,,,,yes,no"))[0].prospective


def test_a_corrupt_ledger_is_refused(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    for bad in ['{"event": "assign"', '{"event": "assign", "tender": "t-0001"}', '{"event": "assign", "tender": '
                '"t-0001", "status": "maybe", "keys": []}', '{"event": "burn", "tender": "t-0009", "from": "heldout",'
                ' "to": "dev"}']:  # fmt: skip
        ledger.write_text(bad + "\n", encoding="utf-8")
        with pytest.raises(sh.SplitError):
            sh.load_ledger(ledger)


@pytest.mark.parametrize(
    ("column", "value"),
    [("property_owner", "N/A"), ("project_family", "saknas"), ("client", "Okänd kund"), ("client", "AB"),
     ("client", "nan"), ("property_owner", "Privat person"), ("client", "Uppgift saknas")],
)  # fmt: skip
def test_placeholders_in_any_column_are_refused(tmp_path: Path, column: str, value: str) -> None:
    cells = {"client": "Kund A", "property_owner": "", "project_family": ""} | {column: value}
    meta = write_meta(tmp_path, f"t-0001,{cells['client']},{cells['property_owner']},{cells['project_family']},no,no")
    with pytest.raises(sh.SplitError):
        sh.read_metadata(meta)


def test_updates_are_written_after_their_burns_and_a_cut_off_run_heals(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    state = held_out_and_dev()
    sh.append_events(ledger, [sh._assign("t-0001", sh.HELDOUT, "hash", state.keys["t-0001"])])
    sh.append_events(ledger, [sh._assign("t-0002", sh.DEV, "hash", state.keys["t-0002"])])
    rows = [row("t-0001", client="Kund A", owner="Kund B"), row("t-0002", "Kund B")]
    events = sh.plan_split(sh.load_ledger(ledger), rows)
    assert [e["event"] for e in events] == ["burn", "update"]
    sh.append_events(ledger, events[:1])  # the run stops after the burn
    healed = sh.plan_split(sh.load_ledger(ledger), rows)
    assert [e["event"] for e in healed] == ["update"]
    sh.append_events(ledger, healed)
    final = sh.load_ledger(ledger)
    assert final.status == {"t-0001": sh.DEV, "t-0002": sh.DEV}
    assert sh.plan_split(final, rows) == []


def test_forced_dev_from_an_update_round_trips_through_the_ledger(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    keys = sh.group_keys("Kund A", "", "")
    sh.append_events(ledger, [sh._assign("t-0001", sh.HELDOUT, "hash", keys)])
    forced = [row("t-0001", client="Kund A", forced=True)]
    sh.append_events(ledger, sh.plan_split(sh.load_ledger(ledger), forced))
    state = sh.load_ledger(ledger)
    assert state.status["t-0001"] == sh.DEV and state.is_forced("t-0001")
    assert sh.plan_split(state, forced) == []
    with pytest.raises(sh.SplitError):
        sh.plan_split(state, [row("t-0001", client="Kund A")])


@pytest.mark.parametrize(
    "event",
    [
        {"event": "assign", "tender": "Projektnamn", "status": "dev", "keys": []},
        {"event": "assign", "tender": "t-0001", "status": "dev", "keys": "abc"},
        {"event": "assign", "tender": "t-0001", "status": "dev", "keys": [], "forced_dev": "false"},
    ],
)
def test_ledger_values_are_type_checked(tmp_path: Path, event: dict) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps(event) + "\n", encoding="utf-8")
    with pytest.raises(sh.SplitError):
        sh.load_ledger(ledger)


@pytest.mark.parametrize("value", ["Okänd AB", "Privatperson AB", "Ej tillämpligt", "Se ovan"])
def test_placeholders_with_a_legal_form_or_as_phrases_are_refused(value: str) -> None:
    with pytest.raises(sh.SplitError):
        sh.group_keys(value, "", "")


@pytest.mark.parametrize(
    "value", ["Brf Exempelgården", "Fiktiv Bygg & Montage AB", "Person Fastigheter AB", "NA Bygg AB"]
)
def test_real_looking_names_are_accepted(value: str) -> None:
    assert sh.group_keys(value, "", "")


def test_a_symlinked_or_contradictory_ledger_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("", encoding="utf-8")
    link = tmp_path / "ledger.jsonl"
    link.symlink_to(target)
    with pytest.raises(sh.SplitError):
        sh.load_ledger(link)
    plain = tmp_path / "plain.jsonl"
    for event in (
        {"event": "assign", "tender": "t-0001", "status": "dev", "keys": [], "prospective": True},
        {"event": "assign", "tender": "t-0001", "status": "heldout", "keys": [], "forced_dev": True},
    ):
        plain.write_text(json.dumps(event) + "\n", encoding="utf-8")
        with pytest.raises(sh.SplitError):
            sh.load_ledger(plain)
    plain.write_text("[" * 100_000 + "\n", encoding="utf-8")
    with pytest.raises(sh.SplitError):
        sh.load_ledger(plain)
