"""Kalkyl end to end against a scripted fake SDK in a temp data folder: R2 with repair, R3, the XLSX, the CalcPost
contract, replay, and the CLI's guards. No network, no key, $0."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import openpyxl
import pytest
from kalkyl_synthetic import (
    GOOD,
    PRICE_STRINGS,
    cite,
    fake_sdk,
    first_block,
    good_handler,
    message,
    req,
    submit,
    text,
    write_catalog,
    write_prep,
)

from anbud import cli, paths
from anbud.config import load_config
from anbud.evals import split_heldout as sh
from anbud.granskning.contracts import CalcPost
from anbud.granskning.corpus import Corpus
from anbud.kalkyl.catalog import ids
from anbud.kalkyl.catalog import load as load_catalog
from anbud.kalkyl.extract import extract_chunk, system_blocks
from anbud.kalkyl.lines import heading_paths, make_chunks
from anbud.kalkyl.run import run_dev_tender
from anbud.kalkyl.settings import EXTRACT, MATCH, load_kalkyl_config
from anbud.model_client import Budget, ModelClient, PrefixDrift
from anbud.tools import own_tools

REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


@pytest.fixture
def tender(data_root: Path) -> Path:
    tender = paths.dev_tender_dir("co-001", "t-0001")
    write_prep(tender / "prep")
    write_catalog(data_root)
    return tender


def _client(sdk: Any, catalog=None, hard_stop: float = 3.0) -> ModelClient:
    return ModelClient(load_config(), sdk, Budget(hard_stop), run_id="r-1", company_id="co-001")


def _run(sdk: Any, **kw: Any) -> dict[str, Any]:
    return run_dev_tender("co-001", "t-0001", sdk=sdk, catalog_name="synthetic", run_id="kalkyl-test", **kw)


def test_the_client_sends_kalkyl_tools_on_sonnet_and_keeps_the_prefix(data_root: Path) -> None:
    write_catalog(data_root)
    catalog = load_catalog("synthetic")
    sdk = fake_sdk()
    client = _client(sdk, catalog)
    system = [{"type": "text", "text": "Synthetic."}]
    messages = [{"role": "user", "content": '<block id="x">y</block>'}]
    client.call(role=EXTRACT, step="s", system=system, messages=messages, max_tokens=100, effort="medium")
    (params,) = sdk.messages.sent
    assert params["model"] == "claude-sonnet-5-5" and params["tool_choice"] == {"type": "auto"}
    assert [t["name"] for t in params["tools"]] == ["submit_quantities"]
    assert not {"thinking", "temperature", "top_p", "top_k"} & params.keys()
    with pytest.raises(PrefixDrift):
        client.call(
            role=EXTRACT,
            step="s",
            system=[{"type": "text", "text": "Other."}],
            messages=messages,
            max_tokens=100,
            effort="medium",
        )
    client.call(
        role=MATCH,
        step="m",
        system=[{"type": "text", "text": "Match."}],
        messages=messages,
        max_tokens=100,
        effort="medium",
    )  # its own prefix, apart from R2's on the same model
    assert [t["name"] for t in sdk.messages.sent[-1]["tools"]] == ["submit_matches"]


def test_kalkyl_tools_are_static_and_carry_no_catalog() -> None:
    blob = json.dumps(own_tools(EXTRACT) + own_tools(MATCH))
    assert not any(p in blob for p in PRICE_STRINGS) and "uttag-dubbelt" not in blob
    assert own_tools(EXTRACT) == own_tools(EXTRACT)


def _setup(data_root: Path):
    write_catalog(data_root)
    catalog = load_catalog("synthetic")
    corpus = Corpus.load(write_prep(data_root / "prep"))
    chunk = make_chunks(corpus, 12000, 30)[0]
    return catalog, corpus, chunk


def _extract(sdk: Any, catalog, corpus, chunk):
    from anbud.kalkyl.extract import chunk_message

    settings = load_kalkyl_config().roles[EXTRACT]
    message_text = chunk_message(chunk, corpus, heading_paths(corpus))
    client = _client(sdk, catalog)
    return extract_chunk(
        client, chunk, corpus, set(ids(catalog)), system_blocks(catalog, corpus), message_text, settings
    )


def test_a_failing_record_gets_one_repair_turn(data_root: Path) -> None:
    catalog, corpus, chunk = _setup(data_root)
    good = req("Downlight", "stated", "st", [cite("aaaaaa-000005", "24 st")], quantity=24)
    wrong = req("Kabelstege", "stated", "m", [cite("aaaaaa-000007", "Kabelstege 54 m")], quantity=54)
    fixed = req("Kabelstege", "stated", "m", [cite("aaaaaa-000007", "Kabelstege 45 m")], quantity=45)
    answers = iter(
        [
            message([text("Läser."), submit({"records": [good, wrong], "set_aside": []})]),
            message([submit({"records": [fixed]}, tool_id="toolu_2")]),
        ]
    )
    sdk = fake_sdk(lambda params: next(answers))
    res = _extract(sdk, catalog, corpus, chunk)
    assert res.status == "submitted" and res.turns == 2 and not res.rejected
    assert [r.what for r, _ in res.records] == ["Downlight", "Kabelstege"]
    second = sdk.messages.sent[1]["messages"]
    (result,) = second[-1]["content"]
    assert result["type"] == "tool_result" and result["is_error"] and "record 2" in result["content"]
    assert second[1]["role"] == "assistant"


def test_a_record_that_fails_twice_is_rejected_never_priced(data_root: Path) -> None:
    catalog, corpus, chunk = _setup(data_root)
    wrong = req("Kabelstege", "stated", "m", [cite("aaaaaa-000007", "Kabelstege 54 m")], quantity=54)
    sdk = fake_sdk(lambda params: message([submit({"records": [wrong]})]))
    res = _extract(sdk, catalog, corpus, chunk)
    assert res.records == [] and len(res.rejected) == 1 and res.rejected[0].block_ids == ["aaaaaa-000007"]
    assert len(sdk.messages.sent) == 2


def test_no_submission_gets_one_nudge_and_a_long_answer_is_truncated(data_root: Path) -> None:
    catalog, corpus, chunk = _setup(data_root)
    sdk = fake_sdk(lambda params: message([text("Inget.")], stop_reason="end_turn"))
    res = _extract(sdk, catalog, corpus, chunk)
    assert res.status == "no_submit" and len(sdk.messages.sent) == 2
    assert "submit_quantities" in sdk.messages.sent[1]["messages"][-1]["content"]
    sdk = fake_sdk(lambda params: message([text("...")], stop_reason="max_tokens"))
    assert _extract(sdk, catalog, corpus, chunk).status == "truncated"


def test_a_model_error_marks_the_chunk_and_keeps_its_message_private(data_root: Path) -> None:
    catalog, corpus, chunk = _setup(data_root)
    error = anthropic.BadRequestError("secret detail", response=httpx2.Response(400, request=REQUEST), body=None)

    def fail(params: dict) -> Any:
        raise error

    res = _extract(fake_sdk(fail), catalog, corpus, chunk)
    assert res.status == "error" and res.error == "bad_request"


def test_a_dev_tender_run_writes_a_priced_calc(tender: Path) -> None:
    sdk = fake_sdk()
    summary = _run(sdk)
    assert summary["complete"] and summary["chunks"] == 2
    assert summary["posts_priced"] == 4 and summary["posts_missing_price"] == 1
    assert summary["lines_not_handled"] == 1 and summary["lines_set_aside_by_model"] == 1
    out = tender / "kalkyl" / "kalkyl-test"
    posts = [CalcPost.model_validate(p) for p in json.loads((out / "posts.json").read_text())]
    by_text = {p.text: p for p in posts}
    assert by_text["Dubbla vägguttag i sovrum"].quantity == 6 and by_text["Dubbla vägguttag i sovrum"].amount == 2268
    assert by_text["Laddstolpe"].section == "Option"
    assert by_text["Lägenhetscentral"].amount is None  # R3 said none: listed, not guessed
    assert "Rörarbeten" not in by_text  # other trade: not a calc line
    result = json.loads((out / "result.json").read_text())
    assert Decimal(result["totals"]["total"]) == Decimal("58953.00")
    assert Decimal(result["totals"]["options_total"]) == Decimal("63600.00")
    # Prompts carry no catalog price, and every call went to Sonnet with Kalkyl's own tools.
    for params in sdk.messages.sent:
        sent = json.dumps(params["system"]) + json.dumps(params["messages"])
        assert not any(re.search(rf"(?<![\d.,]){re.escape(p)}(?![\d])", sent) for p in PRICE_STRINGS)
        assert params["model"] == "claude-sonnet-5-5"
    assert {p["tools"][0]["name"] for p in sdk.messages.sent} == {"submit_quantities", "submit_matches"}
    # Summary and events carry counts, ids and costs, never tender text.
    logged = (out / "summary.json").read_text() + (data_root_of(tender) / "co-001" / "runs").joinpath(
        "kalkyl-test", "events.jsonl"
    ).read_text()
    assert "Downlight" not in logged and "lägenhet" not in logged


def data_root_of(tender: Path) -> Path:
    return tender.parents[2]


def test_the_xlsx_has_the_label_on_every_sheet_citations_and_ej_omhandertaget(tender: Path) -> None:
    _run(fake_sdk())
    wb = openpyxl.load_workbook(tender / "kalkyl" / "kalkyl-test" / "kalkyl.xlsx")
    assert wb.sheetnames == ["Översikt", "Kalkyl", "Underlag", "Objektmodell", "Ej omhändertaget"]
    for ws in wb.worksheets:
        assert ws["A1"].value == "Katalog: Syntetisk testkatalog"
    calc = wb["Kalkyl"]
    heads = [c.value for c in calc[4]]
    rows = [dict(zip(heads, (c.value for c in row), strict=True)) for row in calc.iter_rows(min_row=5) if row[1].value]
    downlight = next(r for r in rows if r["Post"] == "Downlight")
    assert downlight["Källa (block)"] == "aaaaaa-000005" and "24 st" in downlight["Citat"]
    assert downlight["Summa"] == pytest.approx(22560.0) and downlight["Typkod"] == "L1"
    central = next(r for r in rows if r["Post"] == "Lägenhetscentral")
    assert central["Status"] == "Saknar pris" and "R3" in central["Anmärkning"]
    uncovered = wb["Ej omhändertaget"]
    assert uncovered["A5"].value == "bbbbbb-000005" and uncovered["G5"].value.startswith("Bortvalt av modellen")
    objects = wb["Objektmodell"]
    assert objects["A5"].value == "lägenheter" and objects["B5"].value == 3


def test_tender_text_is_never_written_as_a_formula(data_root: Path) -> None:
    tender = paths.dev_tender_dir("co-001", "t-0001")
    from kalkyl_synthetic import BLOCKS

    from anbud.models import Block

    evil = Block(id="aaaaaa-000005", doc_id="aaaaaaaaaaaa", kind="table_row", text="=HYPERLINK(1) | 24 st", page=2)
    write_prep(tender / "prep", [evil if b.id == evil.id else b for b in BLOCKS])
    write_catalog(data_root)
    _run(fake_sdk(lambda params: message([submit({"records": [], "set_aside": []})])))
    ws = openpyxl.load_workbook(tender / "kalkyl" / "kalkyl-test" / "kalkyl.xlsx")["Ej omhändertaget"]
    cell = next(c for row in ws.iter_rows(min_row=5) for c in row if c.value == "=HYPERLINK(1) | 24 st")
    assert cell.data_type == "s"


def test_r3_maps_an_unmapped_post_only_to_a_fitting_item(tender: Path) -> None:
    def handler(params: dict) -> Any:
        if params["tools"][0]["name"] == "submit_matches":
            ids = re.findall(r'<post id="([^"]+)"', params["messages"][0]["content"])
            return message(
                [submit({"matches": [{"post_id": i, "catalog_id": "lgh-central"} for i in ids]}, "submit_matches")]
            )
        return good_handler(params)

    summary = _run(fake_sdk(handler))
    assert summary["posts_priced"] == 5 and summary["posts_missing_price"] == 0
    posts = json.loads((tender / "kalkyl" / "kalkyl-test" / "posts.json").read_text())
    central = next(p for p in posts if p["text"] == "Lägenhetscentral")
    assert central["quantity"] == 3 and central["amount"] == 3 * 2500 + 9 * 650


def test_a_budget_stop_leaves_the_rest_unread_and_the_calc_incomplete(tender: Path) -> None:
    from anbud.kalkyl.settings import KalkylConfig

    tiny = KalkylConfig(**{**vars(load_kalkyl_config()), "hard_stop": 0.0001})
    summary = _run(fake_sdk(), kalkyl=tiny)
    assert not summary["complete"] and summary["chunk_status"] == {"not_run": 2}
    assert summary["lines_not_handled"] == summary["quantity_lines"] + summary["scope_lines"]
    wb = openpyxl.load_workbook(tender / "kalkyl" / "kalkyl-test" / "kalkyl.xlsx")
    assert "OFULLSTÄNDIG" in wb["Kalkyl"]["A2"].value


def test_a_replay_reproduces_the_calc_from_the_step_cache_at_no_cost(tender: Path) -> None:
    paid = _run(fake_sdk())
    out = tender / "kalkyl" / "kalkyl-test"
    first = (out / "posts.json").read_bytes()
    events = data_root_of(tender) / "co-001" / "runs" / "kalkyl-test" / "events.jsonl"
    calls = events.read_text().count('"model_call"')
    summary = _run(None)
    assert summary["complete"] and summary["cost_usd"] == paid["cost_usd"]  # the run's spend, unchanged
    assert events.read_text().count('"model_call"') == calls and '"cache_hit"' in events.read_text()
    assert (out / "posts.json").read_bytes() == first


def test_unverified_quotes_are_never_priced(tender: Path) -> None:
    lie = req("Downlight", "stated", "st", [cite("aaaaaa-000005", "240 st")], quantity=240, catalog_id="downlight")

    def handler(params: dict) -> Any:
        if params["tools"][0]["name"] == "submit_quantities" and first_block(params) == "aaaaaa-000001":
            return message([submit({"records": [lie], "set_aside": []})])
        return good_handler(params)

    summary = _run(fake_sdk(handler))
    assert summary["records_rejected"] == 1 and summary["posts_priced"] == 0
    result = json.loads((tender / "kalkyl" / "kalkyl-test" / "result.json").read_text())
    assert any(u["block_id"] == "aaaaaa-000005" and "underkändes" in u["reason"] for u in result["uncovered"])


def _ledger(status: str) -> None:
    sh.append_events(paths.ledger_path("co-001"), [sh._assign("t-0001", status, "test", ())])


def test_the_cli_refuses_tenders_that_arent_dev(tender: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["kalkyl", "co-001", "t-0001"]) == cli.REFUSED  # not in the ledger
    _ledger(sh.HELDOUT)
    assert cli.main(["kalkyl", "co-001", "t-0001", "--dry-run"]) == cli.REFUSED
    assert "refused" in capsys.readouterr().err


def test_the_cli_dry_run_costs_nothing_and_paid_runs_never_start_in_ci(
    tender: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _ledger(sh.DEV)
    from anbud import model_client

    def no_key(**_: Any) -> str:
        raise AssertionError("the dry run must not read a key")

    monkeypatch.setattr(model_client, "keychain_key", no_key)
    assert cli.main(["kalkyl", "co-001", "t-0001", "--dry-run", "--catalog", "synthetic"]) == 0
    out = capsys.readouterr().out
    assert "chunks: 2" in out and "cost_usd_est_high" in out
    assert cli.main(["kalkyl", "co-001", "t-0001", "--replay"]) == cli.REFUSED  # a replay names its run
    monkeypatch.setenv("CI", "true")
    assert cli.main(["kalkyl", "co-001", "t-0001", "--catalog", "synthetic"]) == cli.REFUSED


def test_the_cli_runs_with_a_fake_sdk(tender: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _ledger(sh.DEV)
    from anbud import model_client

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(model_client, "keychain_key", lambda **_: "sk-test-value")
    monkeypatch.setattr(model_client, "make_sdk_client", lambda config, key: fake_sdk())
    assert cli.main(["kalkyl", "co-001", "t-0001", "--catalog", "synthetic", "--run-id", "kalkyl-cli"]) == 0
    out = capsys.readouterr().out
    assert "posts_priced: 4" in out and "sk-test-value" not in out
    assert (tender / "kalkyl" / "kalkyl-cli" / "kalkyl.xlsx").exists()


def test_good_answers_cover_the_synthetic_tender() -> None:
    assert set(GOOD) == {"aaaaaa-000001", "bbbbbb-000001"}
