"""The old engine's hard stop waits for room instead of ending the run on reservations alone (audit B5, the old
engine). The preload reserves each call's worst case, so with calls in flight a call could end the run (exit 86) while
what the calls really cost still fit. Now it waits for the calls in flight, in arrival order (`Room` in
baseline_usage.mjs), and a call that doesn't fit with nothing in flight still ends the run. Its token estimate errs
high on the measured calls. The `Room` tests run under node or bun, so CI runs them too; the preload's run under bun,
like the other preload tests. No network, synthetic data, $0.

Each test names the one-line mutation it catches; each was made, seen to fail the test, and reverted."""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from anbud.cli import baseline_report
from anbud.config import CONFIG_DIR
from anbud.evals import baseline

USAGE = Path(baseline.PRELOAD).with_name("baseline_usage.mjs")
JS = shutil.which("node") or shutil.which("bun")
BUN = shutil.which("bun")
needs_js = pytest.mark.skipif(JS is None, reason="neither node nor bun is installed")
needs_bun = pytest.mark.skipif(BUN is None, reason="bun isn't installed")
PRICE = tomllib.loads((CONFIG_DIR / "prices.toml").read_text())[baseline.MODEL]


def js(tmp_path: Path, body: str) -> dict:
    """Run `body` as a module beside the usage helpers; it ends with `done({...})`, which prints JSON and exits (a
    waiter's timer would otherwise hold the process open)."""
    script = tmp_path / "t.mjs"
    script.write_text(
        f"import {{ Room, estimateInputTokens }} from '{USAGE.as_uri()}';\n"
        "const done = out => { console.log(JSON.stringify(out)); process.exit(0); };\n"
        "const tick = ms => new Promise(r => setTimeout(r, ms));\n"
        "const pending = (p, ms = 300) => Promise.race([p, tick(ms).then(() => 'still waiting')]);\n" + body
    )
    proc = subprocess.run([JS, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr[-400:]
    return json.loads(proc.stdout)


@needs_js
def test_a_call_that_doesnt_fit_beside_one_in_flight_waits_for_it_and_then_runs(tmp_path: Path) -> None:
    """Mutation: `if (!this.wait || (!this.queue.length && this.inFlight === 0))` -> `if (true)` in `Room.take`: the
    call is refused at once, as before the wait."""
    out = js(
        tmp_path,
        """
const room = new Room(10);
await room.take(6);
const second = room.take(6).then(w => w, e => `refused ${e.message}`);
const before = await pending(second, 50);
room.settle(6, 1);
done({ before, waited: await second, spent: room.spent, reserved: room.reserved, inFlight: room.inFlight });
""",
    )
    assert out == {
        "before": "still waiting",
        "waited": {"reserved_usd": 6, "in_flight": 1},
        "spent": 1,
        "reserved": 6,
        "inFlight": 1,
    }


@needs_js
def test_a_call_that_cant_fit_with_nothing_in_flight_is_refused_at_once_or_after_its_wait(tmp_path: Path) -> None:
    """Mutation: `} else if (this.inFlight === 0) {` -> `} else if (false) {` in `Room.#drain`: the call waits until
    the wait limit for room that never comes."""
    out = js(
        tmp_path,
        """
const room = new Room(10);
const atOnce = await room.take(15).then(() => 'ran', e => ({ waited: e.waited }));
await room.take(6);
const later = room.take(9).then(() => 'ran', e => ({ waited: e.waited }));
room.settle(6, 6);
done({ atOnce, later: await pending(later), queue: room.queue.length, inFlight: room.inFlight });
""",
    )
    assert out == {
        "atOnce": {"waited": None},
        "later": {"waited": {"reserved_usd": 6, "in_flight": 1}},
        "queue": 0,
        "inFlight": 0,
    }


@needs_js
def test_six_calls_with_staggered_settles_go_in_arrival_order(tmp_path: Path) -> None:
    """A later call, even one that would fit, never takes the room ahead of an earlier one, so a large call can't be
    starved by small ones. Mutation: `if (!this.queue.length && this.fits(usd))` -> `if (this.fits(usd))` in
    `Room.take`: the small call `d` overtakes `c`."""
    out = js(
        tmp_path,
        """
const room = new Room(10);
const order = [];
await room.take(5);
await room.take(4);
const ask = (name, usd) => room.take(usd).then(() => order.push(name));
const asked = [ask('c', 6), ask('d', 1), ask('e', 3), ask('f', 2)];
await tick(20);
const before = [...order];
room.settle(5, 0);
await tick(20);
const afterA = [...order];
room.settle(4, 0);
await tick(20);
const afterB = [...order];
room.settle(6, 0);
await Promise.all(asked);
done({ before, afterA, afterB, order, reserved: room.reserved, inFlight: room.inFlight });
""",
    )
    assert out == {
        "before": [],
        "afterA": ["c"],
        "afterB": ["c", "d", "e"],
        "order": ["c", "d", "e", "f"],
        "reserved": 6,
        "inFlight": 3,
    }


@needs_js
def test_a_call_waits_no_longer_than_the_wait_limit_and_then_frees_the_line(tmp_path: Path) -> None:
    """A call in flight that never settles can't hold the run open forever. Mutation: the limit timer's
    `reject(new Refused(usd, waited));` -> deleted: the call waits forever."""
    out = js(
        tmp_path,
        """
const room = new Room(10, { limitMs: 50 });
await room.take(6);
const stuck = await pending(room.take(6).then(() => 'ran', e => ({ waited: e.waited })));
const next = await pending(room.take(3).then(w => ({ ran: w })));
done({ stuck, next, queue: room.queue.length });
""",
    )
    assert out == {"stuck": {"waited": {"reserved_usd": 6, "in_flight": 1}}, "next": {"ran": None}, "queue": 0}


@needs_js
def test_a_waiting_call_its_caller_aborts_leaves_the_line_and_the_next_call_runs(tmp_path: Path) -> None:
    """The app's SDK times out or aborts a call while it waits for room: it leaves the line holding no room, fails
    with the signal's reason as a real fetch would, and the call behind it gets the room. A call whose signal is
    already aborted takes no room at all. Mutation: `signal?.addEventListener('abort', waiter.onAbort, { once: true });`
    -> deleted in `Room.take`: the aborted call keeps its place, and the call behind it waits too. Also caught:
    `if (signal?.aborted) return Promise.reject(abortReason(signal));` -> deleted: an aborted call takes room, and
    `this.#drain(); // the call behind it may fit now` -> deleted: the call behind it waits."""
    out = js(
        tmp_path,
        """
const room = new Room(10);
await room.take(6);
const ac = new AbortController();
const aborted = room.take(6, ac.signal).then(() => 'ran', e => e.name);
const next = room.take(3).then(w => ({ ran: w }));
await tick(20);
const before = room.queue.length;
ac.abort();
const ended = { aborted: await pending(aborted), next: await pending(next) };
const already = await room.take(0.5, AbortSignal.abort()).then(() => 'ran', e => e.name);
done({ before, ...ended, already, queue: room.queue.length, reserved: room.reserved, inFlight: room.inFlight,
  spent: room.spent });
""",
    )
    assert out == {
        "before": 2,
        "aborted": "AbortError",
        "next": {"ran": {"reserved_usd": 6, "in_flight": 1}},
        "already": "AbortError",
        "queue": 0,
        "reserved": 9,
        "inFlight": 2,
        "spent": 0,
    }


# (characters, tokens) measured on the dev tender's paid reading calls, counts only (see test_kalkyl_token_fit.py):
# the densest chunk message, the largest one, and the system blocks with the tools cached.
MEASURED = ((623, 416), (25_045, 12_902), (25_118, 14_624))


@needs_js
def test_the_token_estimate_errs_high_on_the_measured_calls(tmp_path: Path) -> None:
    """Mutation: `const CHARS_PER_TOKEN = 1.4;` -> `const CHARS_PER_TOKEN = 3;` in baseline_usage.mjs, the factor
    before the fit: a call's tokens, and so its reservation, are estimated low."""
    out = js(
        tmp_path,
        f"""
const measured = {json.dumps(MEASURED)};
done(measured.map(([chars]) => estimateInputTokens({{ messages: [{{ role: 'user', content: 'x'.repeat(chars) }}] }})));
""",
    )
    assert all(estimate >= tokens for estimate, (_, tokens) in zip(out, MEASURED, strict=True))


# The preload under bun: an engine that sends its calls at once, like the app's script, against a stub that answers
# each with a made-up stream costing about $0.004 (the same stub as test_baseline.py's).
ENGINE = """
const sizes = JSON.parse(process.env.SIZES);
await Promise.all(sizes.map(async max_tokens => {
  const body = JSON.stringify({ model: 'claude-sonnet-5', max_tokens, stream: true, messages: [] });
  const res = await fetch('https://api.anthropic.com/v1/messages', { method: 'POST', body });
  await res.text();
}));
"""
STUB = """
const usage = { input_tokens: 1000, cache_creation_input_tokens: 0, cache_read_input_tokens: 500, output_tokens: 1 };
const events = [{ type: 'message_start', message: { usage } }, { type: 'message_delta', usage: { output_tokens: 200 } },
  { type: 'message_stop' }];
const sse = events.map(e => `event: ${e.type}\\ndata: ${JSON.stringify(e)}\\n\\n`).join('');
globalThis.fetch = async () => new Response(sse, { status: 200, headers: { 'content-type': 'text/event-stream' } });
"""
HARD_STOP = 1.0


def max_tokens_for(share: float) -> int:
    """max_tokens whose reservation is `share` of the hard stop: an empty message has no input to count."""
    return int(share * HARD_STOP * 1e6 / (1.05 * PRICE["output"]))


def preload(tmp_path: Path, sizes: list[int], engine: str = ENGINE, stub: str = STUB) -> tuple[int, list[dict], Path]:
    (tmp_path / "stub.mjs").write_text(stub)
    (tmp_path / "engine.mjs").write_text(engine)
    log = tmp_path / "calls.jsonl"
    env = {
        "PATH": "/usr/bin:/bin",
        "SIZES": json.dumps(sizes),
        "ANBUD_BASELINE_CALLS": str(log),
        "ANBUD_BASELINE_HARD_STOP": str(HARD_STOP),
        "ANBUD_BASELINE_PRICE": json.dumps(PRICE),
    }
    preloads = [f"--preload={tmp_path / 'stub.mjs'}", f"--preload={baseline.PRELOAD}"]
    command = [BUN, "--no-env-file", *preloads, str(tmp_path / "engine.mjs")]
    proc = subprocess.run(command, env=env, cwd=tmp_path, capture_output=True, timeout=30, check=False)
    return proc.returncode, [json.loads(line) for line in log.read_text().splitlines()], log


@needs_bun
def test_calls_sent_at_once_that_each_reserve_most_of_the_stop_wait_and_the_run_ends_well(tmp_path: Path) -> None:
    """Four calls, each reserving 60% of the hard stop and costing under a cent, sent at once: each waits for the one
    before it, in order, and the run ends well. Mutation: `new Room(hardStop, { wait: true })` -> `{ wait: false }`
    in baseline_preload.mjs: the second call ends the run (exit 86)."""
    code, records, log = preload(tmp_path, [max_tokens_for(0.6)] * 4)
    assert code == 0
    calls = [r for r in records if r["event"] == "call"]
    waits = [r for r in records if r["event"] == "wait"]
    assert [c["seq"] for c in calls] == [1, 2, 3, 4] and all(c["status"] == 200 for c in calls)
    assert [w["seq"] for w in waits] == [2, 3, 4]
    assert all(
        set(w) == {"at", "event", "seq", "model", "ms", "reserve_usd", "reserved_usd", "in_flight"} for w in waits
    )
    assert waits[0]["in_flight"] == 1 and waits[0]["reserved_usd"] == pytest.approx(calls[0]["reserve_usd"])
    counts = baseline.read_calls(log)
    assert counts["calls"] == 4 and counts["calls_waited"] == 3 and counts["calls_failed"] == 0


@needs_bun
def test_a_lone_call_bigger_than_the_stop_still_ends_the_run(tmp_path: Path) -> None:
    """The true out-of-budget stop is kept: a call that can't fit with nothing in flight waits for the call before
    it, then ends the run (exit 86) without being sent. The boundary; under `Room.#drain`'s refusal mutation
    (`} else if (false) {`) it waits until the wait limit instead, past this test's timeout."""
    code, records, _ = preload(tmp_path, [max_tokens_for(0.3), max_tokens_for(1.5)])
    assert code == 86
    events = [r for r in records if r["event"] != "start"]
    assert [(r["event"], r["seq"]) for r in events] == [("call", 1), ("hard_stop", 2)]
    assert events[-1]["in_flight_usd"] == 0 and events[-1]["spent_usd"] == pytest.approx(events[0]["cost_usd"])
    assert "waited_ms" in events[-1]


# The second call is aborted by its caller while it waits behind the first, which the stub holds for a moment.
ABORTING = """
import { writeFileSync } from 'node:fs';
const sizes = JSON.parse(process.env.SIZES);
const ac = new AbortController();
const send = (max_tokens, signal) => fetch('https://api.anthropic.com/v1/messages', { method: 'POST', signal,
  body: JSON.stringify({ model: 'claude-sonnet-5', max_tokens, stream: true, messages: [] }) })
  .then(res => res.text()).then(() => 'ok', error => error.name);
const ends = Promise.all([send(sizes[0]), send(sizes[1], ac.signal), send(sizes[2])]);
setTimeout(() => ac.abort(), 50);
writeFileSync('ends.json', JSON.stringify(await ends));
"""
SLOW_FIRST = """
const usage = { input_tokens: 1000, cache_creation_input_tokens: 0, cache_read_input_tokens: 500, output_tokens: 1 };
const events = [{ type: 'message_start', message: { usage } }, { type: 'message_delta', usage: { output_tokens: 200 } },
  { type: 'message_stop' }];
const sse = events.map(e => `event: ${e.type}\\ndata: ${JSON.stringify(e)}\\n\\n`).join('');
let n = 0;
globalThis.fetch = async () => {
  if (n++ === 0) await new Promise(r => setTimeout(r, 400)); // the first call stays in flight a while
  return new Response(sse, { status: 200, headers: { 'content-type': 'text/event-stream' } });
};
"""


@needs_bun
def test_a_call_aborted_while_it_waits_is_never_sent_and_costs_nothing(tmp_path: Path) -> None:
    """Calls of 60%, 60% and 30% of the stop: the second waits behind the first, and its caller aborts it. It fails
    with an AbortError, is never sent, costs nothing, and the third gets its room before the first ends. Mutation:
    `room.take(reserve, init?.signal ?? (…))` -> `room.take(reserve)` in baseline_preload.mjs: the aborted call keeps
    its place and is sent once the first ends."""
    code, records, _ = preload(
        tmp_path, [max_tokens_for(0.6), max_tokens_for(0.6), max_tokens_for(0.3)], ABORTING, SLOW_FIRST
    )
    assert code == 0
    assert json.loads((tmp_path / "ends.json").read_text()) == ["ok", "AbortError", "ok"]
    calls = [r["seq"] for r in records if r["event"] == "call"]
    assert calls == [3, 1]  # the third ran while the first was still in flight
    aborted = [r for r in records if r["event"] == "aborted"]
    assert [a["seq"] for a in aborted] == [2] and set(aborted[0]) == {
        "at",
        "event",
        "seq",
        "model",
        "ms",
        "reserve_usd",
    }


def test_the_report_says_how_many_calls_waited_and_the_longest_wait() -> None:
    """Mutation: `if result.calls_waited:` -> `if False:` in `cli.baseline_report`: the waits don't show."""
    result = baseline.Outcome("b-1", "ok", 0, 3, {}, calls=4, calls_waited=3, wait_ms_max=41_600)
    assert "waited for room under the hard stop: 3 calls, the longest 42 s" in baseline_report(result)
    assert not any("waited" in line for line in baseline_report(baseline.Outcome("b-2", "ok", 0, 3, {}, calls=4)))
