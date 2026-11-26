// Pure helpers for baseline_preload.mjs: what a Messages API call may cost before it is sent, and what it cost from
// its usage. Prices are USD per million tokens, as in config/prices.toml. Nothing here reads a request's text.

const PAGE_TOKENS = 3000; // a PDF page as text plus image, rounded up
const IMAGE_TOKENS = 1600;
const CHARS_PER_TOKEN = 3; // rounded down for Swedish text, so the estimate errs high

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
