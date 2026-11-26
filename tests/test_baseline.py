"""The old-engine baseline wrapper: export, documents, the run guard, and the preload's call log, hard stop and host
block. Every bun test runs without network: either the run ends before its first call (dry run, hard stop, blocked
host), or a stub preloaded before ours answers fetch with a made-up stream. Every file here is synthetic."""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from anbud import cli, paths
from anbud.config import CONFIG_DIR, Price
from anbud.evals import baseline, freeze
from anbud.evals import split_heldout as sh

BUN = shutil.which("bun")
needs_bun = pytest.mark.skipif(BUN is None, reason="bun isn't installed")
NOW = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)
PRICE = tomllib.loads((CONFIG_DIR / "prices.toml").read_text())[baseline.MODEL]


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def fake_app(tmp_path: Path, script: str) -> Path:
    """A git repo shaped like the app: the script, a tsconfig, an env file and a bunfig that must not be exported, and
    an untracked node_modules."""
    app = tmp_path / "app"
    (app / "scripts").mkdir(parents=True)
    (app / "scripts" / "run-anbud-local.ts").write_text(script)
    (app / "tsconfig.json").write_text("{}")
    (app / ".env").write_text("NOT_FOR_THE_ENGINE=1\n")
    (app / "sub").mkdir()
    (app / "sub" / ".env.local").write_text("NOT_FOR_THE_ENGINE=1\n")
    (app / "bunfig.toml").write_text("[install]\n")
    (app / ".gitignore").write_text("node_modules\n")
    git(app, "init", "-q", "-b", "main")
    git(app, "add", "-A", "-f")
    git(app, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "app")
    (app / "node_modules").mkdir()
    return app


ENGINE = """
import { readdirSync } from 'node:fs';
const args = process.argv.slice(2);
const folder = args[args.indexOf('--documents') + 1];
console.log(`files ${readdirSync(folder).length}`);
const body = JSON.stringify({ model: 'claude-sonnet-5', max_tokens: 10, stream: true, messages: [] });
for (let i = 0; i < Number(process.env.CALLS ?? 1); i++) {
  const res = await fetch('https://api.anthropic.com/v1/messages', { method: 'POST', body });
  await res.text();
}
"""


def documents(tmp_path: Path) -> list[Path]:
    folder = tmp_path / "docs"
    (folder / "a").mkdir(parents=True)
    (folder / "b").mkdir()
    files = [folder / "a" / "AF.pdf", folder / "b" / "af.PDF", folder / "a" / "lista.xlsx", folder / "a" / "E.dwg"]
    for path in files:
        path.write_bytes(b"x")
    return files


def test_flat_documents_keeps_what_the_engine_reads_and_renames_clashes(tmp_path: Path) -> None:
    count, skipped = baseline.flat_documents(documents(tmp_path), tmp_path / "flat")
    assert count == 3 and skipped == {"dwg": 1}
    names = sorted(p.name for p in (tmp_path / "flat").iterdir())
    assert names == ["AF.pdf", "af (2).PDF", "lista.xlsx"]


def test_the_export_drops_env_files_and_bunfig_and_links_node_modules(tmp_path: Path) -> None:
    app = fake_app(tmp_path, ENGINE)
    sha = baseline.resolve_app(app, "HEAD")
    root = tmp_path / "data"
    exported = baseline.export_app(app, sha, root)
    assert exported == root / "baseline" / "app" / sha
    assert (exported / baseline.SCRIPT).is_file() and (exported / "tsconfig.json").is_file()
    assert not (exported / ".env").exists() and not (exported / "sub" / ".env.local").exists()
    assert not (exported / "bunfig.toml").exists()
    assert (exported / "node_modules").is_symlink()
    (app / "scripts" / "run-anbud-local.ts").write_text("// uncommitted")
    assert baseline.export_app(app, sha, root) == exported  # built once per commit; the working copy never matters
    assert (exported / baseline.SCRIPT).read_text() == ENGINE


def test_read_calls_counts_and_keeps_ids_only(tmp_path: Path) -> None:
    log = tmp_path / "calls.jsonl"
    records = [
        {"event": "start", "env_names": ["HOME", "PATH"]},
        {"event": "call", "status": 200, "cost_usd": 0.5, "ids": {"anthropic-workspace-id": "w1", "request-id": "r1"}},
        {"event": "call", "status": 529, "cost_usd": 0, "ids": {"anthropic-workspace-id": "w1"}},
        {"event": "call", "status": 200, "cost_usd": 0.25, "incomplete": True, "ids": {}},
    ]
    log.write_text("".join(json.dumps(r) + "\n" for r in records) + "{broken\n")
    counts = baseline.read_calls(log)
    assert counts["calls"] == 3 and counts["calls_failed"] == 1 and counts["calls_incomplete"] == 1
    assert counts["cost_usd"] == 0.75 and counts["workspace_ids"] == ["w1"] and counts["env_names"] == ["HOME", "PATH"]


def test_the_hard_stop_can_be_lowered_but_never_raised(data_root: Path, tmp_path: Path) -> None:
    with pytest.raises(baseline.BaselineError, match="never raise"):
        baseline.run_dev(
            "co-001", "t-0001", dry_run=True, hard_stop=1000.0, app=tmp_path, code={}, stop_requested=lambda _: False
        )


def test_a_sealed_tender_is_refused_before_anything_runs(data_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sh.append_events(paths.ledger_path("co-001"), [sh._assign("t-0002", sh.HELDOUT, "test", ())])
    assert cli.main(["baseline", "co-001", "t-0002", "--dry-run"]) == cli.REFUSED
    assert "only dev tenders" in capsys.readouterr().err
    assert not (data_root / "baseline").exists()


def run(tmp_path: Path, *, dry_run: bool, hard_stop: float = 5.0, script: str = ENGINE) -> baseline.Outcome:
    app = fake_app(tmp_path, script)
    root = tmp_path / "data"
    exported = baseline.export_app(app, baseline.resolve_app(app, "HEAD"), root)
    return baseline.run_baseline(
        run_id="baseline-t-0001-test",
        documents=documents(tmp_path),
        outputs_dir=root / "out",
        record_dir=root / "record",
        app_root=exported,
        hard_stop=hard_stop,
        timeout_s=120,
        dry_run=dry_run,
        api_key=lambda: "test-key-never-sent",
        manifest={"kind": "baseline"},
        stop_requested=lambda: False,
    )


@needs_bun
def test_a_dry_run_reaches_the_first_call_and_sends_nothing(tmp_path: Path) -> None:
    result = run(tmp_path, dry_run=True)
    assert result.outcome == "dry_run" and result.exit_code == 88 and result.calls == 0
    assert result.documents == 3 and result.skipped_by_type == {"dwg": 1}
    # The child's whole environment: no app env file, no key but the one given, nothing of the parent's.
    assert set(result.env_names) <= {"ANTHROPIC_API_KEY", "ANTHROPIC_MODEL_MAIN", "HOME", "PATH", "TMPDIR"}
    record = tmp_path / "data" / "record"
    assert not (record / "work").exists()  # the copied documents are gone
    assert not (tmp_path / "data" / "out").exists()  # no engine output, no outputs folder
    assert json.loads((record / "run.json").read_text())["status"] == "dry_run"
    assert "files 3" in (record / "engine.log").read_text()
    assert "test-key-never-sent" not in (record / "calls.jsonl").read_text() + (record / "run.json").read_text()


@needs_bun
def test_a_call_past_the_hard_stop_is_never_sent(tmp_path: Path) -> None:
    result = run(tmp_path, dry_run=False, hard_stop=0.00001)
    assert result.outcome == "hard_stop" and result.exit_code == 86 and result.calls == 0


@needs_bun
def test_any_other_host_ends_the_run(tmp_path: Path) -> None:
    script = "await fetch('https://example.invalid/x');"
    result = run(tmp_path, dry_run=False, script=script)
    assert result.outcome == "blocked" and result.exit_code == 87 and result.blocked_hosts == 1


STUB = """
const usage = { input_tokens: 1000, cache_creation_input_tokens: 0, cache_read_input_tokens: 500, output_tokens: 1 };
const events = [
  { type: 'message_start', message: { usage } },
  { type: 'content_block_delta', delta: { type: 'input_json_delta', partial_json: '{}' } },
  { type: 'message_delta', usage: { output_tokens: 200 } },
  { type: 'message_stop' },
];
const sse = events.map(e => `event: ${e.type}\\ndata: ${JSON.stringify(e)}\\n\\n`).join('');
globalThis.fetch = async () => new Response(sse, { status: 200, headers: {
  'content-type': 'text/event-stream', 'request-id': 'req_1', 'anthropic-organization-id': 'org_1',
  'anthropic-workspace-id': 'wrkspc_1', 'anthropic-ratelimit-requests-limit': '50' } });
"""


def stubbed(tmp_path: Path, calls: int, hard_stop: float) -> tuple[int, list[dict]]:
    (tmp_path / "stub.mjs").write_text(STUB)
    (tmp_path / "engine.mjs").write_text(ENGINE.replace("readdirSync(folder).length", "0"))
    log = tmp_path / "calls.jsonl"
    env = {
        "PATH": "/usr/bin:/bin",
        "CALLS": str(calls),
        "ANBUD_BASELINE_CALLS": str(log),
        "ANBUD_BASELINE_HARD_STOP": str(hard_stop),
        "ANBUD_BASELINE_PRICE": json.dumps(PRICE),
    }
    preloads = [f"--preload={tmp_path / 'stub.mjs'}", f"--preload={baseline.PRELOAD}"]
    command = [BUN, "--no-env-file", *preloads, str(tmp_path / "engine.mjs"), "--documents", "."]
    proc = subprocess.run(command, env=env, cwd=tmp_path, capture_output=True, timeout=60, check=False)
    return proc.returncode, [json.loads(line) for line in log.read_text().splitlines()]


@needs_bun
def test_each_call_is_logged_with_ids_usage_and_the_harness_price(tmp_path: Path) -> None:
    code, records = stubbed(tmp_path, calls=2, hard_stop=5.0)
    assert code == 0
    calls = [r for r in records if r["event"] == "call"]
    assert len(calls) == 2
    expected = Price(**PRICE).cost(
        SimpleNamespace(
            input_tokens=1000, output_tokens=200, cache_creation_input_tokens=0, cache_read_input_tokens=500
        )
    )
    for call in calls:
        assert call["status"] == 200 and call["usage"]["output_tokens"] == 200
        assert call["cost_usd"] == pytest.approx(expected)
        assert call["ids"] == {
            "request-id": "req_1",
            "anthropic-organization-id": "org_1",
            "anthropic-workspace-id": "wrkspc_1",
        }
    start = next(r for r in records if r["event"] == "start")
    assert not any(n.startswith("ANBUD_BASELINE_") for n in start["env_names"])  # removed before the engine loads


@needs_bun
def test_settled_spend_ends_the_run_at_the_hard_stop(tmp_path: Path) -> None:
    # Each call costs about $0.0041 but its body promises far less, so the second still fits under the stop by its
    # estimate; the third is refused on settled spend.
    code, records = stubbed(tmp_path, calls=5, hard_stop=0.006)
    assert code == 86
    events = [r for r in records if r["event"] != "start"]
    assert [r["event"] for r in events] == ["call", "call", "hard_stop"]
    assert events[-1]["spent_usd"] == pytest.approx(2 * events[0]["cost_usd"])


FINISHING = """
import { writeFileSync } from 'node:fs';
const args = process.argv.slice(2);
writeFileSync(args[args.indexOf('--out') + 1], JSON.stringify({ total: 1234567, rows: [{}, {}] }));
const sv = n => n.toLocaleString('sv-SE');
console.log(`\\nTotal: ${sv(1234567)} kr (arbete ${sv(1000000)}, material ${sv(234567)}, 812.5 h)`);
"""


def dev_tender(data_root: Path) -> Path:
    tender = paths.dev_tender_dir("co-001", "t-0001")
    (tender / "inbox" / "a").mkdir(parents=True)
    (tender / "inbox" / "a" / "AF.pdf").write_bytes(b"x")
    (tender / "inbox.sha256").write_text(f"{'0' * 64}  a/AF.pdf\n")
    return tender


def snapshot(folder: Path) -> list[str]:
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*"))


@needs_bun
def test_a_baseline_run_lands_under_predictions_and_nothing_under_dev(data_root: Path, tmp_path: Path) -> None:
    tender = dev_tender(data_root)
    before = snapshot(tender)
    app = fake_app(tmp_path, FINISHING)
    result = baseline.run_dev(
        "co-001",
        "t-0001",
        dry_run=False,
        app=app,
        app_ref="HEAD",
        code={"harness_sha": "x"},
        stop_requested=lambda _: False,
        api_key=lambda: "test-key-never-sent",
        now=NOW,
    )
    assert result.outcome == "ok" and result.calls == 0
    assert snapshot(tender) == before  # the build lane reads dev/: nothing of the old engine goes there
    assert not (data_root / "co-001" / "runs").exists()
    run_dir = paths.baseline_dir("co-001", "t-0001") / result.run_id
    assert run_dir.is_relative_to(data_root / "predictions" / "co-001" / "baseline")
    assert (run_dir / "outputs" / "engine.json").is_file() and (run_dir / "record" / "engine.log").is_file()

    frozen = freeze.freeze_run(
        "co-001", result.run_id, outputs_dir=run_dir / "outputs", run_record_dir=run_dir / "record", tender_id="t-0001"
    )
    rel = f"predictions/co-001/baseline/t-0001/{result.run_id}"
    assert frozen["outputs_dir"] == f"{rel}/outputs" and frozen["run_record_dir"] == f"{rel}/record"
    totals = json.loads(baseline.write_totals(run_dir).read_text())
    assert totals == {
        "total_sek": 1234567,
        "rows": 2,
        "labor_sek": 1000000,
        "material_sek": 234567,
        "hours": 812.5,
        "printed_total_matches": True,
    }
    assert snapshot(tender) == before


def test_a_moved_frozen_run_is_relocated_only_when_every_hash_matches(data_root: Path) -> None:
    outputs = data_root / "co-001" / "dev" / "t-0001" / "baseline" / "r1"
    record = data_root / "co-001" / "runs" / "r1"
    for folder, name in ((outputs, "engine.json"), (record, "run.json")):
        folder.mkdir(parents=True)
        (folder / name).write_text("{}")
    freeze.freeze_run("co-001", "r1", outputs_dir=outputs, run_record_dir=record, tender_id="t-0001", now=NOW)
    new = paths.baseline_dir("co-001", "t-0001") / "r1"
    new.mkdir(parents=True)
    outputs.rename(new / "outputs")
    record.rename(new / "record")

    (new / "record" / "extra.txt").write_text("x")
    with pytest.raises(freeze.FreezeError, match="don't match"):
        freeze.relocate_run("co-001", "r1", outputs_dir=new / "outputs", run_record_dir=new / "record")
    (new / "record" / "extra.txt").unlink()
    with pytest.raises(freeze.FreezeError, match="no frozen run"):
        freeze.relocate_run("co-001", "r2", outputs_dir=new / "outputs", run_record_dir=new / "record")

    assert (
        cli.main(
            ["freeze", "relocate", "co-001", "r1", "--outputs", str(new / "outputs"), "--record", str(new / "record")]
        )
        == 0
    )
    summary = freeze.verify(freeze.ledger_file("co-001"))
    assert summary.lines == 2 and summary.kinds == {"freeze": 1, "relocate": 1}
    last = json.loads(freeze.ledger_file("co-001").read_text().splitlines()[-1])
    assert last["outputs_dir"] == "predictions/co-001/baseline/t-0001/r1/outputs" and last["frozen_line"] == 1
    assert freeze.freezes_by_tender(freeze.ledger_file("co-001")) == {"co-001/t-0001": 1}
