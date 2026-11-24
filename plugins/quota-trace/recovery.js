import { randomBytes, createHash } from 'node:crypto';

// In-memory mappings only: local session rows and message bodies are untouched.
export class SessionRecovery {
  constructor(now = Date.now, mint = () => 'ses_' + randomBytes(32).toString('hex')) {
    this.now = now;
    this.mint = mint;
    this.entries = new Map();
  }
  key(event) { return JSON.stringify([event.sessionID, event.model?.providerID, event.model?.id]); }
  prune() {
    for (const [key, entry] of this.entries) {
      if (this.now() - entry.touched > 86400000) this.entries.delete(key);
    }
  }
  begin(event) {
    if (event.model?.providerID !== 'opencode' || !event.model?.id?.endsWith('-free') ||
        event.kind && event.kind !== 'primary' ||
        event.error?.type !== 'provider.free-limit' || event.error?.status !== 429 ||
        event.attempt > 2 || !event.sessionID) return false;
    this.prune();
    const key = this.key(event);
    const entry = this.entries.get(key);
    if (entry && (entry.pending || this.now() < entry.retryAt)) return false;
    if (!entry && this.entries.size >= 4096) return false;
    this.entries.set(key, { current: entry?.current, pending: this.mint(),
      retryAt: this.now() + 600000, touched: this.now() });
    event.decision = { retry: true, delay: 1000 };
    return true;
  }
  apply(event) {
    this.prune();
    const entry = this.entries.get(this.key(event));
    const replacement = entry?.pending || entry?.current;
    if (!replacement) return false;
    entry.touched = this.now();
    const headers = event.headers;
    // OpenCode adds parent affinity on subagents; leaving it intact would undo
    // the replacement. Keep a distinct child identity with consistent ancestry.
    const child = headers['x-opencode-parent-session-id'] || headers['x-parent-session-id'];
    for (const key of ['x-session-affinity', 'X-Session-Id', 'x-opencode-session']) headers[key] = replacement;
    headers['x-opencode-session-id'] = child
      ? 'ses_' + createHash('sha256').update(replacement + ':' + event.sessionID).digest('hex')
      : replacement;
    for (const key of ['x-opencode-parent-session-id', 'x-parent-session-id']) {
      if (headers[key]) headers[key] = replacement;
    }
    return true;
  }
  response(event) {
    const entry = this.entries.get(this.key(event));
    if (!entry?.pending) return null;
    const success = event.response.status >= 200 && event.response.status < 300;
    if (success) {
      entry.current = entry.pending;
      entry.retryAt = this.now() + 30000;
    }
    entry.pending = undefined;
    entry.touched = this.now();
    return success;
  }
  failure(event) {
    const entry = this.entries.get(this.key(event));
    if (entry?.pending && event.error?.type !== undefined) entry.pending = undefined;
  }
}
