// Loaded with `bun --preload` before the app's scripts/run-anbud-local.ts, so it wraps fetch underneath the script's
// own wrapper. `anbud baseline` starts it; see src/anbud/evals/baseline.py.
//
// - Only https://api.anthropic.com/v1/messages is reachable. Any other request ends the run (exit 87).
// - Every model call is logged to ANBUD_BASELINE_CALLS as one JSON line: sequence, model, max_tokens, HTTP status,
//   duration, the id headers of the response (request, organization, workspace), usage and cost, and for an error
//   response its kind (`errorKind`). Never a prompt, a response text or a header value other than those ids.
// - The hard stop: a call whose worst case would take settled plus in-flight spend past ANBUD_BASELINE_HARD_STOP
//   waits for the calls in flight, in arrival order (`Room`), and is logged as a `wait`. A call that doesn't fit with
//   nothing in flight is never sent, and the run ends (exit 86). A call its caller aborts while it waits is never
//   sent either: it fails as a real fetch would, costs nothing, and is logged as `aborted`.
// - With ANBUD_BASELINE_DRY=1 the first model call ends the run before any network (exit 88).
import { appendFileSync } from 'node:fs';
import { Refused, Room, headerIds, maxCost, mergeUsage, usageCost } from './baseline_usage.mjs';

const EXIT = { hard_stop: 86, blocked: 87, dry_run: 88 };
const API = 'https://api.anthropic.com/v1/messages';
const env = process.env;
const log = env.ANBUD_BASELINE_CALLS;
const hardStop = Number(env.ANBUD_BASELINE_HARD_STOP);
const price = JSON.parse(env.ANBUD_BASELINE_PRICE ?? 'null');
const dry = env.ANBUD_BASELINE_DRY === '1';
if (!log || !(hardStop > 0) || !price) {
  console.error('baseline preload: ANBUD_BASELINE_CALLS, _HARD_STOP and _PRICE are required');
  process.exit(EXIT.blocked);
}
for (const name of Object.keys(env)) if (name.startsWith('ANBUD_BASELINE_')) delete env[name];

// An error response's kind: `credit_balance` when the API says the balance can't pay, else the API's own error type
// (`authentication_error`, `invalid_request_error`, ...). Never its message. The exam's breaker reads it.
const errorKind = async response => {
  try {
    const body = await response.clone().json();
    if (/credit balance/i.test(String(body?.error?.message ?? ''))) return 'credit_balance';
    const type = body?.error?.type;
    return typeof type === 'string' && /^[a-z_]{1,40}$/.test(type) ? type : null;
  } catch {
    return null;
  }
};

const write = record => appendFileSync(log, JSON.stringify({ at: new Date().toISOString(), ...record }) + '\n', { mode: 0o600 });
const end = (code, record) => {
  write(record);
  process.exit(code);
};
write({ event: 'start', env_names: Object.keys(env).sort(), hard_stop_usd: hardStop, dry_run: dry });

let seq = 0;
const room = new Room(hardStop, { wait: true });
const realFetch = globalThis.fetch;

globalThis.fetch = async (input, init) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input?.url;
  let parsed = null;
  try {
    parsed = new URL(String(url));
  } catch {
    /* refused below */
  }
  if (!parsed || parsed.origin + parsed.pathname !== API) {
    end(EXIT.blocked, { event: 'blocked', host: parsed?.host ?? null });
  }
  const n = ++seq;
  let body = null;
  try {
    body = typeof init?.body === 'string' ? JSON.parse(init.body) : null;
  } catch {
    body = null;
  }
  const model = typeof body?.model === 'string' ? body.model : null;
  const maxTokens = Number(body?.max_tokens) || null;
  if (dry) end(EXIT.dry_run, { event: 'dry_run', seq: n, model, max_tokens: maxTokens });
  const reserve = body ? maxCost(body, price) : hardStop;
  const asked = Date.now();
  let waited = null;
  try {
    waited = await room.take(reserve, init?.signal ?? (input instanceof Request ? input.signal : undefined));
  } catch (error) {
    if (!(error instanceof Refused)) {
      write({ event: 'aborted', seq: n, model, ms: Date.now() - asked, reserve_usd: reserve });
      throw error;
    }
    end(EXIT.hard_stop, { event: 'hard_stop', seq: n, model, reserve_usd: reserve, spent_usd: room.spent, in_flight_usd: room.reserved, waited_ms: Date.now() - asked });
  }
  if (waited) write({ event: 'wait', seq: n, model, ms: Date.now() - asked, reserve_usd: reserve, reserved_usd: waited.reserved_usd, in_flight: waited.in_flight });
  const started = Date.now();
  const record = { event: 'call', seq: n, model, max_tokens: maxTokens, reserve_usd: reserve };
  let settled = false;
  const settle = (usage, extra) => {
    if (settled) return;
    settled = true;
    // A cut-off call is counted at its worst case; an error response is not billed.
    const cost = extra.incomplete ? reserve : usage ? usageCost(usage, price) : extra.status >= 400 ? 0 : reserve;
    room.settle(reserve, cost);
    write({ ...record, ...extra, ms: Date.now() - started, usage: usage ?? null, cost_usd: cost, spent_usd: room.spent });
  };
  let response;
  try {
    response = await realFetch(input, init);
  } catch (error) {
    settle(null, { status: null, error: error?.name ?? 'Error', incomplete: true });
    throw error;
  }
  const ids = headerIds(response.headers);
  if (!response.ok || !response.body) {
    settle(null, { status: response.status, ids, ...(response.ok ? {} : { error_kind: await errorKind(response) }) });
    return response;
  }
  const type = response.headers.get('content-type') ?? '';
  if (!type.includes('text/event-stream')) {
    const copy = response.clone();
    copy.json().then(
      json => settle(json?.usage ?? null, { status: response.status, ids }),
      () => settle(null, { status: response.status, ids, incomplete: true }),
    );
    return response;
  }
  let usage = {}, buffer = '';
  const decoder = new TextDecoder();
  const tap = new TransformStream({
    transform(chunk, controller) {
      controller.enqueue(chunk);
      buffer += decoder.decode(chunk, { stream: true }).replace(/\r\n/g, '\n');
      let k;
      while ((k = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, k);
        buffer = buffer.slice(k + 2);
        const data = frame.split('\n').filter(l => l.startsWith('data:')).map(l => l.slice(5).trim()).join('\n');
        if (!data) continue;
        try {
          usage = mergeUsage(usage, JSON.parse(data));
        } catch {
          /* a partial frame */
        }
      }
    },
    flush() {
      settle(Object.keys(usage).length ? usage : null, { status: response.status, ids });
    },
    cancel() {
      settle(Object.keys(usage).length ? usage : null, { status: response.status, ids, incomplete: true });
    },
  });
  return new Response(response.body.pipeThrough(tap), { status: response.status, statusText: response.statusText, headers: response.headers });
};
