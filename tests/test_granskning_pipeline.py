"""`anbud run` end to end on a synthetic tender with a scripted fake SDK: page cards, map, lenses, checks, merge,
verifier, kronor and the report files; then `--replay` of the same run at $0. No network, no key."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from granskning_fakes import ScriptedSDK, first_text, png_bytes, reply, usage, write_prep
from openpyxl import Workbook, load_workbook
from test_runguard import ledger_with

from anbud import cli, model_client
from anbud.evals import split_heldout as sh
from anbud.granskning import command, provenance
from anbud.granskning.catalog import Catalog, CatalogItem, calc_hourly_rate, with_calc_rate
from anbud.granskning.contracts import CalcPost, RunInputs
from anbud.granskning.items import Kronor
from anbud.granskning.merge import MERGE_SYSTEM
from anbud.granskning.page_cards import SYSTEM as CARDS_SYSTEM
from anbud.models import Flag
from anbud.store import RunStore

RUN = "t-0001-e2e"
CLEAN = provenance.GitState("a" * 40, False, None)


@pytest.fixture(autouse=True)
def clean_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """Paid runs need a clean checkout; the tests' own tree may be mid-edit."""
    monkeypatch.setattr(provenance, "git_state", lambda root=None: CLEAN)


VITE = {"block_id": "aaaaaa-000002", "quote": "Etappvite utgår med 25 000 kr per påbörjad vecka"}
CARD_TEXT = "Planritning, elplan plan 1\n\nCentral A1 | Gruppförteckning\n\nTeckenförklaring: NG närvarogivare"


def _flag(claim_type: str, claim: str, citation: dict[str, str], basis: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return {
        "lens_id": "x",
        "claim_type": claim_type,
        "claim": claim,
        "citations": [citation],
        "money_basis": basis,
        **kw,
    }


VITE_FLAG = _flag(
    "contract_vite",
    "Etappvitet är 25 000 kr per vecka och etapp, utan tak.",
    VITE,
    {
        "kind": "contract_formula",
        "formula": "vite_per_week",
        "formula_inputs": [{"name": "per_week", "value": 25000}, {"name": "weeks", "value": 4}],
    },
    contractual_exposure=True,
)
DEADLINE_FLAG = _flag(
    "contract_deadline",
    "Vitet löper per påbörjad vecka.",
    VITE,
    {"kind": "contract_formula", "formula": "vite_per_week"},
)
PRESENCE_FLAG = _flag(
    "unpriced_requirement",
    "Närvarostyrning i trapphusen saknas i kalkylen.",
    {"block_id": "bbbbbb-000002", "quote": "Trapphusbelysning med närvarostyrning"},
    {"kind": "missing_post", "item": "Närvarogivare", "quantity": 40, "unit": "st"},
)
CARD_FLAG = _flag(
    "trade_boundary",
    "Central A1 på elplanen finns inte i kalkylen.",
    {"block_id": "cccccc-p1-t1-2", "quote": "Central A1"},
    {"kind": "hours_gap", "hours": 30},
)
GENERIC_FLAG = _flag(
    "generic_scope",
    "Förberedelse för laddplatser i garaget saknas i kalkylen.",
    {"block_id": "bbbbbb-000003", "quote": "Laddinfrastruktur för elbilsladdplatser i garaget"},
    {"kind": "hours_gap", "hours": 60},
)
BAD_TYPE_FLAG = _flag("not_a_type", "Något.", VITE, {"kind": "hours_gap", "hours": 50})

LENS_FLAGS = {
    "contract_terms": [VITE_FLAG, DEADLINE_FLAG],
    "quantities_hours": [PRESENCE_FLAG],
    "trade_boundaries": [CARD_FLAG],
    "generic_scope": [GENERIC_FLAG],
}


def _map() -> dict[str, Any]:
    return {
        "documents": [{"doc_id": "aaaaaaaaaaaa", "role": "AF"}, {"doc_id": "bbbbbbbbbbbb", "role": "teknisk el"}],
        "stated_scope": "Elarbeten i ett påhittat flerbostadshus.",
        "documents_named_not_present": ["Gränsdragningslista"],
        "lenses": [{"lens_id": "contract_terms", "focus_questions": ["Vite?", "Tak?", "Etapper?"]}],
        "citations": [{"block_id": "aaaaaa-000002", "quote": "Etappvite"}],
    }


def _last_tool_result(params: dict[str, Any]) -> str:
    content = params["messages"][-1]["content"]
    if isinstance(content, str):
        return content
    return " ".join(str(b.get("content", b.get("text", ""))) for b in content)


def script(params: dict[str, Any]) -> Any:
    if params["system"] == CARDS_SYSTEM:
        return reply(CARD_TEXT, use=usage(input_tokens=1500, output_tokens=80))
    if params["system"][0]["text"] == MERGE_SYSTEM:
        payload = json.loads(re.search(r"<clusters>\n(.*)\n</clusters>", first_text(params), re.S).group(1))
        findings = [f for cluster in payload for f in cluster["findings"]]
        merged = next(f for f in findings if f["claim_type"] == "contract_vite")
        return reply(("submit_flags", {"flags": [merged]}))
    text = first_text(params)
    if "Map this tender" in text:
        return reply(("submit_map", _map()), use=usage(write=40_000))
    if "You are the verifier" in text:
        fp = re.search(r'fingerprint "([0-9a-f]{64})"', text).group(1)
        refuted = "Central A1" in text
        verdict = {
            "fingerprint": fp,
            "verdict": "refuted" if refuted else "confirmed",
            "electrical_scope": True,
            "covered_by_calc": False,
            "would_be_wrong_if": "En annan handling flyttar omfattningen.",
        }
        return reply(("submit_verdict", verdict), use=usage(read=40_000))
    lens = re.search(r"review lens `([a-z_]+)`", text).group(1)
    if lens == "regulation" and len(params["messages"]) == 1:
        return reply(("submit_flags", {"flags": [BAD_TYPE_FLAG]}), use=usage(read=40_000))
    return reply(("submit_flags", {"flags": LENS_FLAGS.get(lens, [])}), use=usage(read=40_000))


def _calc(folder: Path) -> None:
    folder.mkdir(parents=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Kalkyl"
    ws.append(["Benämning", "Antal", "Enhet", "À-pris", "Summa"])
    ws.append(["Timpris montör", 1, "tim", 700, 700])
    ws.append(["Trapphusarmatur", 35, "st", 900, 31500])
    wb.save(folder / "kalkyl.xlsx")


def _catalog(data_root: Path) -> None:
    catalog = Catalog(
        name="estim-default",
        label="Syntetisk katalog, antaget timpris 500 kr/h, ej kundens",
        hourly_rate=500,
        items=[CatalogItem(id="ng", name="Närvarogivare", unit="st", unit_price=600, hours=0.8)],
    )
    (data_root / "catalogs").mkdir()
    (data_root / "catalogs" / "estim-default.json").write_text(catalog.model_dump_json())


@pytest.fixture
def tender(data_root: Path) -> Path:
    ledger_with(data_root, t_0001=sh.DEV)
    blob = RunStore("co-001").put_blob(png_bytes())
    base = data_root / "co-001" / "dev" / "t-0001"
    write_prep(base / "prep", tile_blob=blob)
    _calc(base / "calc")
    _catalog(data_root)
    return base


def test_anbud_run_end_to_end_then_replay_at_zero(
    tender: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sdk = ScriptedSDK(script)
    monkeypatch.setattr(model_client, "keychain_key", lambda **_: "sk-test")
    monkeypatch.setattr(model_client, "make_sdk_client", lambda config, key: sdk)
    assert cli.main(["run", "co-001", "t-0001", "--run-id", RUN]) == 0
    out = capsys.readouterr().out

    # Every Opus call sent one prefix, and the card text reached it before the map.
    opus = [p for p in sdk.sent if p["model"] == "claude-opus-5-5"]
    assert all(p["system"] == opus[0]["system"] and p["tools"] == opus[0]["tools"] for p in opus)
    assert "cccccc-p1-t1-2" in opus[0]["system"][2]["text"]
    roles = {"cards": 0, "verifier": 0, "merge": 0}
    for p in sdk.sent:
        if p["system"] == CARDS_SYSTEM:
            roles["cards"] += 1
        elif p["system"][0]["text"] == MERGE_SYSTEM:
            roles["merge"] += 1
        elif "You are the verifier" in first_text(p):
            roles["verifier"] += 1
    assert roles == {"cards": 1, "verifier": 4, "merge": 1}

    run_dir = tender / "runs" / RUN
    flags = [Flag.model_validate(f) for f in json.loads((run_dir / "flags.json").read_text())]
    assert len(flags) == 4  # vite and its duplicate merged; presence, card and generic
    assert "not_a_type" not in {f.claim_type for f in flags}
    kronor = {k: Kronor.model_validate(v) for k, v in json.loads((run_dir / "kronor.json").read_text()).items()}
    items = {i["flag"]["claim_type"]: i for i in json.loads((run_dir / "items.json").read_text())}
    assert items["contract_vite"]["placement"] == "shown" and items["contract_vite"]["lenses"] == ["contract_terms"]
    assert kronor[items["contract_vite"]["fingerprint"]].point == 100_000
    presence = kronor[items["unpriced_requirement"]["fingerprint"]]
    assert presence.point == 40 * (0.8 * 700 + 600)  # the calc's own hourly rate, not the catalog's 500
    assert "från kundens kalkyl" in presence.label and "antaget" not in presence.label
    assert items["trade_boundary"]["placement"] == "refuted"
    assert items["generic_scope"]["placement"] == "generic"
    inputs = RunInputs.model_validate_json((run_dir / "inputs.json").read_text())
    assert inputs.tender_id == "t-0001" and len(inputs.calc_posts) == 2

    page = (run_dir / "report" / "granskning.html").read_text(encoding="utf-8")
    assert "Etappvitet är 25 000 kr" in page and "Gränsdragningslista" in page
    wb = load_workbook(run_dir / "report" / "granskning.xlsx")
    assert wb["Flaggor"].max_row == 3 and wb["Generiska"].max_row == 2
    about = {r[0].value: r[1].value for r in wb["Om rapporten"].iter_rows()}
    assert about["Modellkostnad (USD)"] > 0  # the run's own spend, from the budget

    lens_calls = [p for p in sdk.sent if p["system"] != CARDS_SYSTEM]
    repair = next(p for p in lens_calls if "lens `regulation`" in first_text(p) and len(p["messages"]) == 3)
    assert "claim_type 'not_a_type' is not one of the claim types" in _last_tool_result(repair)

    # The terminal shows ids, statuses, counts and costs only.
    assert "Etappvite" not in out and "närvaro" not in out.lower() and "Central A1" not in out
    assert "verifier: 4 flags" in out and "2 shown, 1 generic" in out

    # The run's manifest and summary record the code it ran with, and the prefix as measured and as estimated.
    manifest = json.loads((RunStore("co-001").run_dir(RUN) / "run.json").read_text())
    paid_code = manifest["code"]
    assert paid_code["harness_sha"] == "a" * 40 and paid_code["dirty"] is False
    granskning = json.loads((RunStore("co-001").run_dir(RUN) / "granskning.json").read_text())
    assert granskning["code"] == paid_code
    prefix = granskning["prefix"]
    assert prefix["measured_tokens"] == 40_000 and prefix["tier_from"] == "measured"  # the map's cache write
    assert prefix["estimated_tokens"] == int(prefix["chars"] / 1.6) and granskning["prefix_tokens"] == 40_000
    assert "prefix 40000 tokens (measured)" in out

    # Replay: no key, no SDK, every answer from the step cache, the same outputs.
    def no_key(**_: Any) -> str:
        raise AssertionError("replay must not read the key")

    monkeypatch.setattr(model_client, "keychain_key", no_key)
    names = ("flags.json", "kronor.json", "items.json", "report/granskning.html")
    before = {name: (run_dir / name).read_text() for name in names}
    summary = RunStore("co-001").run_dir(RUN) / "granskning.json"
    paid_summary = summary.read_text()
    sent = len(sdk.sent)
    assert cli.main(["run", "co-001", "t-0001", "--run-id", RUN, "--replay"]) == 0
    assert "total $0.00" in capsys.readouterr().out
    replay = run_dir / "replay-1"
    for name in names[:3]:
        assert (replay / name).read_text() == before[name]  # the same outputs, in their own folder
    replay_about = load_workbook(replay / "report" / "granskning.xlsx")["Om rapporten"]
    assert {r[0].value: r[1].value for r in replay_about.iter_rows()}["Modellkostnad (USD)"] == 0
    assert {name: (run_dir / name).read_text() for name in names} == before  # the paid run is untouched
    assert len(sdk.sent) == sent
    assert json.loads((RunStore("co-001").run_dir(RUN) / "run.json").read_text())["code"] == paid_code
    replay_summary = json.loads((RunStore("co-001").run_dir(RUN) / "granskning-replay-1.json").read_text())
    assert replay_summary["code"]["harness_sha"] == "a" * 40

    # A replay that misses the cache (here: a different lens set changes the map's request) stops at the map,
    # exits non-zero, and still leaves the paid run alone.
    narrowed = ["run", "co-001", "t-0001", "--run-id", RUN, "--replay", "--lens", "regulation"]
    assert cli.main(narrowed) == command.REPLAY_INCOMPLETE
    captured = capsys.readouterr()
    assert "replay incomplete: 1 calls" in captured.err and "stopping before the lenses: ReplayMiss" in captured.out
    assert (run_dir / "replay-2" / "report" / "granskning.html").exists()
    assert {name: (run_dir / name).read_text() for name in names} == before
    assert summary.read_text() == paid_summary


def test_a_marking_in_a_page_card_stops_the_run(
    tender: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def marked(params: dict[str, Any]) -> Any:
        if params["system"] == CARDS_SYSTEM:
            return reply("HEMLIG enligt säkerhetsskyddslagen")
        raise AssertionError("nothing runs after a marking")

    sdk = ScriptedSDK(marked)
    monkeypatch.setattr(model_client, "keychain_key", lambda **_: "sk-test")
    monkeypatch.setattr(model_client, "make_sdk_client", lambda config, key: sdk)
    assert cli.main(["run", "co-001", "t-0001", "--run-id", "t-0001-m"]) == cli.REFUSED
    assert "security marking" in capsys.readouterr().err
    assert not (tender / "runs" / "t-0001-m" / "report").exists()


def test_the_calc_rate_wins_and_says_so() -> None:
    posts = [
        CalcPost(id="c-1", text="Kabel", quantity=10, unit="m", unit_price=40),
        CalcPost(id="c-2", text="Timpris elmontör", quantity=1, unit="tim", amount=680),
    ]
    assert calc_hourly_rate(posts) == 680
    assert calc_hourly_rate([CalcPost(id="c-3", text="Timpris", unit_price=5)]) is None  # not a plausible rate
    base = Catalog(
        name="estim-default", label="Estims standardkatalog, antaget timpris 650 kr/h, ej kundens", hourly_rate=650
    )
    moved = with_calc_rate(base, posts)
    assert moved.hourly_rate == 680 and moved.label == "Estims standardkatalog, timpris 680 kr/h från kundens kalkyl"
    assert with_calc_rate(base, posts[:1]) is base


def test_a_dry_run_counts_without_a_key_or_a_call(
    tender: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_key(**_: Any) -> str:
        raise AssertionError("a dry run must not read the key")

    monkeypatch.setattr(model_client, "keychain_key", no_key)
    assert cli.main(["run", "co-001", "t-0001", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "8 blocks, 3 documents, 2 calc posts" in out and "tiles for page cards: 1" in out
    assert "lens tier (from the estimate): 8 turns, $1.50 per lens; 8 lenses" in out
    assert "hourly rate from the calc" in out and "estimated prefix:" in out and "at 1.6 chars/token" in out
    assert "a paid run is allowed" in out
    assert "Etappvite" not in out and not (tender / "runs").exists()


@pytest.mark.parametrize(
    ("state", "why"),
    [
        (provenance.GitState("b" * 40, True, "c" * 64), "needs a clean tree"),
        (provenance.GitState(None, True, None), "needs a git checkout"),
    ],
)
def test_a_paid_run_on_a_dirty_or_unversioned_tree_is_refused(
    tender: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state: provenance.GitState,
    why: str,
) -> None:
    def no_key(**_: Any) -> str:
        raise AssertionError("a refused run must not read the key")

    monkeypatch.setattr(provenance, "git_state", lambda root=None: state)
    monkeypatch.setattr(model_client, "keychain_key", no_key)
    assert cli.main(["run", "co-001", "t-0001", "--run-id", "t-0001-d"]) == cli.REFUSED
    assert why in capsys.readouterr().err
    assert not RunStore("co-001").run_dir("t-0001-d").exists() and not (tender / "runs").exists()
    assert cli.main(["run", "co-001", "t-0001", "--dry-run"]) == 0  # a dry run may run dirty
    out = capsys.readouterr().out
    assert "a paid run is refused on this tree" in out
    assert state.diff_sha256 is None or f"diff {'c' * 12}" in out
