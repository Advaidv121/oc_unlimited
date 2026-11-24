import { appendFileSync, mkdirSync, renameSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { createHmac, randomBytes } from 'node:crypto';

const salt = randomBytes(32);
const correlate = (value) => value ? createHmac('sha256', salt).update(String(value)).digest('hex').slice(0, 16) : null;
const directory = join(homedir(), 'Library/Application Support/OpenCodeProxyQuotaGuard');
const logfile = join(directory, 'request-trace.jsonl');

// A failed stream may already have run upstream. Permit one transport retry,
// then stop replaying it. Never turn an upstream free-tier rejection into a retry.
export function boundTransportRetries(event) {
  if (event.error?.type === 'provider.transport' && event.attempt >= 3) {
    event.decision = { retry: false };
    return true;
  }
  return false;
}

// Deliberately select fields; never serialize a request, error, header values,
// tool schema, prompt, or response body into the trace.
export function contextMetadata(event) {
  const options = {};
  for (const key of ['maxTokens', 'temperature', 'topP', 'topK']) {
    if (typeof event.options?.[key] === 'number') options[key] = event.options[key];
  }
  let textChars = 0;
  let parts = 0;
  for (const message of event.messages || []) {
    for (const part of Array.isArray(message.content) ? message.content : []) {
      parts++;
      if (typeof part.text === 'string') textChars += part.text.length;
    }
  }
  return { messageCount: event.messages?.length || 0, partCount: parts,
    textChars, toolCount: Object.keys(event.tools || {}).length, options };
}

export default {
  id: 'oc.quota-trace',
  async setup(ctx) {
    const registrations = [];
    const requests = new WeakMap();
    function write(event, fields) {
      try {
        mkdirSync(directory, { recursive: true, mode: 0o700 });
        try {
          if (statSync(logfile).size > 2 * 1024 * 1024) renameSync(logfile, logfile + '.1');
        } catch {}
        appendFileSync(logfile, JSON.stringify({ time: new Date().toISOString(),
          session: correlate(event.sessionID), model: event.model?.id,
          provider: event.model?.providerID, kind: event.kind, ...fields }) + '\n', { mode: 0o600 });
      } catch { /* Diagnostics must not fail a model request. */ }
    }
    async function hook(name, fn) {
      registrations.push(await ctx.session.hook(name, fn, { providerID: 'opencode' }));
    }
    await hook('context', (event) => write(event, { event: 'context', ...contextMetadata(event) }));
    await hook('model.request', (event) => write(event, {
      event: 'model-request', headerNames: Object.keys(event.headers || {}).map(x => x.toLowerCase()).sort(),
      affinity: correlate(event.headers?.['x-session-affinity']),
      parent: correlate(event.headers?.['x-opencode-parent-session-id']),
      client: event.headers?.['x-opencode-client'],
    }));
    await hook('http.request', (event) => {
      requests.set(event.request, Date.now());
      const url = new URL(event.request.url);
      write(event, { event: 'http-request', host: url.hostname, path: url.pathname,
        headerNames: [...event.request.headers.keys()].sort() });
    });
    await hook('http.response', (event) => {
      const start = requests.get(event.request);
      requests.delete(event.request);
      // This duration ends at response headers, not at the end of generation.
      const headers = {};
      for (const name of ['retry-after', 'retry-after-ms', 'x-should-retry']) {
        const value = event.response.headers.get(name);
        if (value !== null) headers[name] = value.slice(0, 100);
      }
      write(event, { event: 'http-response', status: event.response.status,
        timeToHeadersMs: start === undefined ? null : Date.now() - start, headers });
    });
    await hook('retry', (event) => {
      const proposedRetry = event.decision?.retry;
      const proposedDelayMs = event.decision?.delay;
      const bounded = boundTransportRetries(event);
      write(event, { event: 'retry',
      errorType: event.error?.type, status: event.error?.status,
      retryAfterMs: event.error?.retryAfterMs, attempt: event.attempt,
      proposedRetry, proposedDelayMs, boundedTransportRetry: bounded,
      effectiveRetry: event.decision?.retry });
    });
    write({}, { event: 'loaded', version: ctx.app?.version });
    return async () => {
      for (const registration of registrations) await registration.dispose();
    };
  },
};
