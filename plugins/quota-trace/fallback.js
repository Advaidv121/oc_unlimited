// Use available allowance on another free model through OpenCode's own retry
// loop. No credential, session identity, request-body, or route rewriting.
const ORDER = ['hy4-flash-free', 'deepseek-v4.1-flash-free', 'mimo-v2.6-flash-free'];
export function normalizeModels(result) {
  return Array.isArray(result) ? result : Array.isArray(result?.data) ? result.data : [];
}
export function eligibleModel(model, source) {
  if (!ORDER.includes(model.id) || model.providerID !== 'opencode' || !model.enabled ||
      model.status === 'deprecated' || model.type && model.type !== 'chat' ||
      !model.capabilities?.tools || !model.capabilities.output?.includes('text') ||
      !model.cost?.length || model.cost.some(tier =>
        [tier.input, tier.output, tier.cache?.read, tier.cache?.write].some(cost => cost !== 0))) return false;
  // Do not force a large/image-bearing conversation into a smaller/text-only model.
  // Conservatively preserve the source model's input capacity and modalities.
  if (!source || !source.capabilities?.input?.every(type => model.capabilities.input?.includes(type))) return false;
  const sourceInput = source.limit?.input ?? (source.limit?.context - source.limit?.output);
  const candidateInput = model.limit?.input ?? (model.limit?.context - model.limit?.output);
  return Number.isFinite(sourceInput) && sourceInput > 0 && candidateInput >= sourceInput;
}

export class FreeModelFallback {
  constructor(now = Date.now) {
    this.now = now;
    this.routes = new Map();
    this.requests = new Map();
    this.locks = new Set();
  }
  requestKey(event) { return JSON.stringify([event.sessionID, event.model?.id, event.agent]); }
  noteRequest(event) {
    const key = this.requestKey(event);
    const last = this.requests.get(key);
    this.requests.set(key, { kind: event.kind, at: this.now(),
      // Retry carries no kind. Fail closed if this agent/model also generated
      // auxiliary traffic recently; an auxiliary error must not reroute a task.
      ambiguous: Boolean(last && this.now() - last.at < 120000 &&
        (last.ambiguous || last.kind !== event.kind)) });
    this.prune();
  }
  prune() {
    for (const [key, entry] of this.routes) if (this.now() - entry.touched > 86400000) this.routes.delete(key);
    for (const [key, entry] of this.requests) if (this.now() - entry.at > 3600000) this.requests.delete(key);
  }
  async resolve(event, list) {
    if (event.kind !== 'primary' || event.model?.providerID !== 'opencode') return null;
    const route = this.routes.get(event.sessionID);
    if (!route) return null;
    if (route.selected !== event.model.id) {
      // An explicit model selection begins a new chain. Keep known cooldowns,
      // but do not route a user's different selection through the old target.
      route.selected = event.model.id;
      route.target = null;
      route.switches = 0;
      route.tried = new Set();
      route.touched = this.now();
      return null;
    }
    if (!route.target) return null;
    const models = normalizeModels(await list());
    const target = models.find(m => m.id === route.target && m.providerID === 'opencode');
    const source = models.find(m => m.id === event.model.id && m.providerID === 'opencode');
    if (!target || !eligibleModel(target, source) || (route.blocked.get(target.id) || 0) > this.now()) return null;
    const from = event.model.id;
    // Variants are model-specific; preserve none from the failed model.
    event.model = { providerID: 'opencode', id: target.id };
    route.touched = this.now();
    return { from, to: target.id };
  }
  async retry(event, list) {
    const observed = this.requests.get(this.requestKey(event));
    if (event.attempt === 2 && observed?.kind === 'primary' && !observed.ambiguous) {
      const route = this.routes.get(event.sessionID);
      if (route) { route.switches = 0; route.tried = new Set(); }
    }
    const quota = event.error?.type === 'provider.free-limit' && event.error.status === 429;
    const stalled = event.error?.type === 'provider.transport' && event.attempt === 3;
    if ((!quota && !stalled) || event.model?.providerID !== 'opencode' ||
        !ORDER.includes(event.model.id) || !observed || observed.kind !== 'primary' || observed.ambiguous ||
        !Number.isInteger(event.attempt) || event.attempt < 2 || event.attempt > 3 ||
        this.locks.has(event.sessionID)) return null;
    this.locks.add(event.sessionID);
    try {
      const models = normalizeModels(await list());
      const source = models.find(m => m.providerID === 'opencode' && m.id === event.model.id);
      if (!source) return null;
      let route = this.routes.get(event.sessionID);
      if (!route) {
        if (this.routes.size >= 4096) return null;
        route = { selected: event.model.id, target: null, blocked: new Map(), switches: 0,
          tried: new Set(), touched: this.now() };
        this.routes.set(event.sessionID, route);
      }
      if (event.attempt === 2) { route.switches = 0; route.tried = new Set(); }
      route.touched = this.now();
      route.tried.add(event.model.id);
      const retryDelay = Number.isFinite(event.error.retryAfterMs) && event.error.retryAfterMs > 0
        ? event.error.retryAfterMs : 600000;
      route.blocked.set(event.model.id, this.now() + (quota ? Math.min(retryDelay, 86400000) : 120000));
      if (route.switches >= 2) return null;
      const target = ORDER.map(id => models.find(m => m.id === id && m.providerID === 'opencode'))
        .find(m => m && !route.tried.has(m.id) && (route.blocked.get(m.id) || 0) <= this.now() && eligibleModel(m, source));
      if (!target) return null;
      route.target = target.id;
      route.switches++;
      event.decision = { retry: true, delay: 1000 };
      return { from: event.model.id, to: target.id, reason: quota ? 'free-tier-rejection' : 'repeated-transport-failure',
        switches: route.switches };
    } finally { this.locks.delete(event.sessionID); }
  }
}
