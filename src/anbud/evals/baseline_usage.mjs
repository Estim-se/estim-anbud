// Pure helpers for baseline_preload.mjs: what a Messages API call may cost before it is sent, and what it cost from
// its usage. Prices are USD per million tokens, as in config/prices.toml. Nothing here reads a request's text.

const PAGE_TOKENS = 3000; // a PDF page as text plus image, rounded up
const IMAGE_TOKENS = 1600;
// Swedish tender text with markup, as Kalkyl's paid calls measured it (kalkyl/run.py CHARS_PER_TOKEN: 1.50-2.30
// characters per token); the smallest, rounded down, so the estimate errs high. 3 ran low.
const CHARS_PER_TOKEN = 1.4;
// The longest a call waits for room before the run ends at the hard stop anyway: past the app's SDK's own 10-minute
// call timeout, so a wait that long means a call in flight that never settled.
const WAIT_LIMIT_MS = 15 * 60 * 1000;

function pdfPages(base64) {
  try {
    const raw = Buffer.from(base64, 'base64').toString('latin1');
    return Math.max(1, (raw.match(/\/Type\s*\/Page(?![a-z])/g) ?? []).length);
  } catch {
    return 100;
  }
}

function blockTokens(block) {
  if (typeof block === 'string') return block.length / CHARS_PER_TOKEN;
  if (!block || typeof block !== 'object') return 0;
  const source = block.source;
  if (block.type === 'document' && source?.type === 'base64' && typeof source.data === 'string') {
    return source.media_type === 'application/pdf' ? pdfPages(source.data) * PAGE_TOKENS : source.data.length / 4;
  }
  if (block.type === 'image') return IMAGE_TOKENS;
  let tokens = 0;
  for (const [key, value] of Object.entries(block)) {
    if (key === 'source') continue;
    if (typeof value === 'string') tokens += value.length / CHARS_PER_TOKEN;
    else if (Array.isArray(value)) for (const v of value) tokens += blockTokens(v);
    else if (value && typeof value === 'object') tokens += JSON.stringify(value).length / CHARS_PER_TOKEN;
  }
  return tokens;
}

/** Input tokens a request body may use, erring high. */
export function estimateInputTokens(body) {
  if (!body || typeof body !== 'object') return 0;
  let tokens = 0;
  for (const message of body.messages ?? []) {
    const content = message?.content;
    if (typeof content === 'string') tokens += content.length / CHARS_PER_TOKEN;
    else if (Array.isArray(content)) for (const block of content) tokens += blockTokens(block);
  }
  const system = body.system;
  if (typeof system === 'string') tokens += system.length / CHARS_PER_TOKEN;
  else if (Array.isArray(system)) for (const block of system) tokens += blockTokens(block);
  if (body.tools) tokens += JSON.stringify(body.tools).length / CHARS_PER_TOKEN;
  return Math.ceil(tokens);
}

/** The most a call may cost: every input token at the dearest input rate, all of max_tokens used, plus 5%. */
export function maxCost(body, price) {
  const dearest = Math.max(price.input, price.cache_write_5m, price.cache_write_1h);
  const maxTokens = Number(body?.max_tokens) || 0;
  return (1.05 * (estimateInputTokens(body) * dearest + maxTokens * price.output)) / 1e6;
}

/** What a response cost, from its usage. Cache writes without a 5m/1h split are charged at the 1h rate. */
export function usageCost(usage, price) {
  if (!usage) return 0;
  const written = usage.cache_creation_input_tokens ?? 0;
  const split = usage.cache_creation ?? null;
  const write5m = split?.ephemeral_5m_input_tokens ?? 0;
  let write1h = split?.ephemeral_1h_input_tokens ?? 0;
  write1h += Math.max(0, written - write5m - write1h);
  return (
    ((usage.input_tokens ?? 0) * price.input +
      (usage.output_tokens ?? 0) * price.output +
      write5m * price.cache_write_5m +
      write1h * price.cache_write_1h +
      (usage.cache_read_input_tokens ?? 0) * price.cache_read) /
    1e6
  );
}

/** Merge a streamed event's usage into the running usage (message_start, then message_delta's running totals). */
export function mergeUsage(current, event) {
  const usage = event?.type === 'message_start' ? event.message?.usage : event?.type === 'message_delta' ? event.usage : null;
  if (!usage) return current;
  const out = { ...current };
  for (const [key, value] of Object.entries(usage)) {
    if (typeof value === 'number') out[key] = value;
    else if (key === 'cache_creation' && value && typeof value === 'object') out[key] = { ...value };
  }
  return out;
}

/** Response headers worth keeping: ids only (request, organization, workspace), never rate limits or anything else. */
export function headerIds(headers) {
  const out = {};
  for (const [name, value] of headers) {
    const lower = name.toLowerCase();
    if (lower === 'request-id' || (lower.startsWith('anthropic-') && lower.endsWith('-id'))) out[lower] = value;
  }
  return out;
}

/** Why an aborted call ends: the signal's own reason, as a real fetch rejects with, else an AbortError. */
const abortReason = signal => signal.reason ?? new DOMException('This operation was aborted', 'AbortError');

/** A call the hard stop refuses: it doesn't fit with nothing else in flight, or it waited past the limit. */
export class Refused extends Error {
  constructor(reserve, waited) {
    super('hard stop');
    this.reserve = reserve;
    this.waited = waited;
  }
}

/**
 * The hard stop's room, shared by the calls in flight. Each call reserves the most it can cost (`maxCost`) and
 * settles what it did cost. With `wait`, a call that doesn't fit while others are in flight waits for them, in
 * arrival order: a later call never takes the room ahead of an earlier one. It is refused only when it doesn't fit
 * with nothing in flight, or after `limitMs`. Reservations alone then never end a run below its stop. A caller that
 * aborts its call (`signal`) while it waits leaves the line at once, holding no room: it was never sent.
 */
export class Room {
  constructor(hardStop, { wait = true, limitMs = WAIT_LIMIT_MS } = {}) {
    this.hardStop = hardStop;
    this.wait = wait;
    this.limitMs = limitMs;
    this.spent = 0;
    this.reserved = 0;
    this.inFlight = 0; // a count, so a float's residue never holds a wait
    this.queue = []; // the calls waiting for room, oldest first
  }

  fits(usd) {
    return this.spent + this.reserved + usd <= this.hardStop;
  }

  /** Resolves when the call may go, with how it waited (null: it didn't); rejects with Refused, or with the
   * signal's reason when the caller aborts the call first. */
  take(usd, signal) {
    if (signal?.aborted) return Promise.reject(abortReason(signal));
    if (!this.queue.length && this.fits(usd)) {
      this.#grant(usd);
      return Promise.resolve(null);
    }
    if (!this.wait || (!this.queue.length && this.inFlight === 0)) return Promise.reject(new Refused(usd, null));
    const waited = { reserved_usd: this.reserved, in_flight: this.inFlight };
    return new Promise((resolve, reject) => {
      const waiter = { usd, waited, resolve, reject, signal };
      waiter.timer = setTimeout(() => {
        this.#leave(waiter);
        reject(new Refused(usd, waited));
        this.#drain();
      }, this.limitMs);
      waiter.onAbort = () => {
        this.#leave(waiter);
        reject(abortReason(signal));
        this.#drain(); // the call behind it may fit now
      };
      signal?.addEventListener('abort', waiter.onAbort, { once: true });
      this.queue.push(waiter);
    });
  }

  /** A reserved call ended, whatever the way: its reservation is released and what it cost is spent. */
  settle(reserved, actual) {
    this.reserved -= reserved;
    this.spent += actual;
    this.inFlight -= 1;
    this.#drain();
  }

  #grant(usd) {
    this.reserved += usd;
    this.inFlight += 1;
  }

  #leave(waiter) {
    const i = this.queue.indexOf(waiter);
    if (i >= 0) this.queue.splice(i, 1);
    clearTimeout(waiter.timer);
    waiter.signal?.removeEventListener('abort', waiter.onAbort);
  }

  #drain() {
    while (this.queue.length) {
      const head = this.queue[0];
      if (this.fits(head.usd)) {
        this.#leave(head);
        this.#grant(head.usd);
        head.resolve(head.waited);
      } else if (this.inFlight === 0) {
        this.#leave(head);
        head.reject(new Refused(head.usd, head.waited)); // nothing left to settle: it never fits
      } else {
        return;
      }
    }
  }
}
