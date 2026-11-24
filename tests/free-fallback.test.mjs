import { test } from 'node:test';
import assert from 'node:assert/strict';
import { FreeModelFallback, eligibleModel } from '../plugins/quota-trace/fallback.js';

const model = (id, patch = {}) => ({ id, providerID: 'opencode', enabled: true, status: 'active',
  capabilities: { tools: true, input: ['text'], output: ['text'] },
  limit: { context: 200000, input: 160000, output: 32000 },
  cost: [{ input: 0, output: 0, cache: { read: 0, write: 0 } }], ...patch });
const models = [model('deepseek-v4.1-flash-free'), model('hy4-flash-free'), model('mimo-v2.6-flash-free')];
const failure = (id = models[0].id, attempt = 2) => ({ sessionID: 'chat', agent: 'build',
  model: { providerID: 'opencode', id, variant: 'high' }, attempt,
  error: { type: 'provider.free-limit', status: 429, retryAfterMs: 60000 }, decision: { retry: false } });
const observe = (fallback, e, kind = 'primary') => fallback.noteRequest({ ...e, kind });

test('a quota rejection reroutes the next primary attempt with history identity intact', async () => {
  const f = new FreeModelFallback(); const e = failure(); observe(f, e);
  assert.deepEqual(await f.retry(e, () => ({ data: models })),
    { from: models[0].id, to: models[1].id, reason: 'free-tier-rejection', switches: 1 });
  const resolve = { ...e, kind: 'primary' };
  await f.resolve(resolve, () => models);
  assert.deepEqual(resolve.model, { providerID: 'opencode', id: models[1].id });
  assert.equal(resolve.sessionID, 'chat');
  const title = { ...e, kind: 'title' }; await f.resolve(title, () => models);
  assert.equal(title.model.id, models[0].id);
});

test('model failures in a chain are attempted only once and stop after two alternatives', async () => {
  const f = new FreeModelFallback(); const first = failure(); observe(f, first);
  await f.retry(first, () => models);
  const second = failure(models[1].id, 3); observe(f, second);
  assert.equal((await f.retry(second, () => models)).to, models[2].id);
  const third = failure(models[2].id, 4); observe(f, third);
  assert.equal(await f.retry(third, () => models), null);
  assert.deepEqual(third.decision, { retry: false });
});

test('transport failure allows the existing retry before selecting an alternative', async () => {
  const f = new FreeModelFallback(); const e = failure(); e.error = { type: 'provider.transport' }; observe(f, e);
  assert.equal(await f.retry(e, () => models), null);
  e.attempt = 3;
  assert.equal((await f.retry(e, () => models)).to, models[1].id);
});

test('rejects paid, disabled, incompatible-media and insufficient-context models', () => {
  const source = models[0];
  for (const patch of [{ enabled: false }, { type: 'decision' },
    { cost: [{ input: 1, output: 0, cache: { read: 0, write: 0 } }] },
    { limit: { context: 32000, output: 8000 } }, { cost: [] },
    { capabilities: { tools: false, input: ['text'], output: ['text'] } }]) {
    assert.equal(eligibleModel(model(models[1].id, patch), source), false);
  }
  assert.equal(eligibleModel(models[1], model(source.id, { capabilities: { input: ['text', 'image'] } })), false);
});

test('auxiliary and concurrent ambiguous requests do not override the primary task', async () => {
  const f = new FreeModelFallback(); const e = failure(); observe(f, e, 'title');
  assert.equal(await f.retry(e, () => models), null);
  observe(f, e, 'primary'); assert.equal(await f.retry(e, () => models), null);
  const child = { ...e, sessionID: 'child' }; observe(f, child);
  assert(await f.retry(child, () => models));
  assert.equal(f.routes.has('chat'), false);
});

test('two simultaneous retry decisions cannot consume two fallback attempts', async () => {
  const f = new FreeModelFallback(); const e = failure(); observe(f, e);
  let release; const pending = new Promise(r => { release = r; });
  const first = f.retry(e, async () => { await pending; return models; });
  assert.equal(await f.retry({ ...e }, () => models), null);
  release(); assert(await first);
});

test('catalog changes and an explicit new selection are respected', async () => {
  const f = new FreeModelFallback(); const e = failure(); observe(f, e); await f.retry(e, () => models);
  const request = { ...e, kind: 'primary' };
  await f.resolve(request, () => [models[0]]);
  assert.equal(request.model.id, models[0].id);
  request.model = { providerID: 'opencode', id: models[2].id };
  assert.equal(await f.resolve(request, () => models), null);
  assert.equal(f.routes.get('chat').selected, models[2].id);
  assert.equal(f.routes.get('chat').target, null);
  const nextFailure = failure(models[2].id); observe(f, nextFailure);
  assert.equal((await f.retry(nextFailure, () => models)).to, models[1].id);
});

test('a new transport failure chain resets a previous turn switch budget', async () => {
  let now = 0; const f = new FreeModelFallback(() => now); const e = failure(); observe(f, e);
  await f.retry(e, () => models); f.routes.get('chat').switches = 2;
  now = 120001;
  const transport = failure(models[1].id); transport.error = { type: 'provider.transport' }; observe(f, transport);
  assert.equal(await f.retry(transport, () => models), null);
  transport.attempt = 3;
  assert.equal((await f.retry(transport, () => models)).to, models[0].id);
});
