import { test } from 'node:test';
import assert from 'node:assert/strict';
import plugin, { contextMetadata, boundTransportRetries } from '../plugins/quota-trace/index.js';

test('metadata counts text without saving it or unapproved options', () => {
  const result = contextMetadata({ messages: [{ content: [{ text: 'secret' }, { type: 'image' }] }],
    tools: { read: { description: 'private schema' } },
    options: { maxTokens: 500, temperature: 0.4, apiKey: 'private key', headers: 'private headers' } });
  assert.deepEqual(result, { messageCount: 1, partCount: 2, textChars: 6, toolCount: 1,
    options: { maxTokens: 500, temperature: 0.4 } });
  assert(!JSON.stringify(result).includes('private'));
  assert(!JSON.stringify(result).includes('secret'));
});

test('registers passive hooks and disposes all registrations', async () => {
  const hooks = new Map();
  let disposed = 0;
  const cleanup = await plugin.setup({ app: { version: 'test' }, session: {
    hook: async (name, fn, scope) => {
      assert.deepEqual(scope, { providerID: 'opencode' });
      hooks.set(name, fn);
      return { dispose: async () => { disposed++; } };
    },
  } });
  assert.deepEqual([...hooks.keys()], ['context', 'experimental.model.resolve', 'model.request', 'http.request', 'http.response', 'retry']);
  const event = { error: { type: 'provider.free-limit', status: 429 }, decision: { retry: false } };
  await hooks.get('retry')(event);
  assert.deepEqual(event.decision, { retry: false });
  await cleanup();
  assert.equal(disposed, 6);
});

test('permits one transport retry and vetoes subsequent replays', () => {
  const event = { error: { type: 'provider.transport' }, attempt: 2,
    decision: { retry: true, delay: 2000 } };
  assert.equal(boundTransportRetries(event), false);
  assert.equal(event.decision.retry, true);
  event.attempt = 3;
  assert.equal(boundTransportRetries(event), true);
  assert.deepEqual(event.decision, { retry: false });
});

test('preserves free-tier rejection and other retry decisions', () => {
  for (const type of ['provider.free-limit', 'provider.rate-limit', 'provider.internal']) {
    const event = { error: { type }, attempt: 5, decision: { retry: false } };
    assert.equal(boundTransportRetries(event), false);
    assert.deepEqual(event.decision, { retry: false });
  }
});

test('registered hooks recover a rejected step by resolving a free alternative', async () => {
  const hooks = new Map();
  const info = id => ({ id, providerID: 'opencode', enabled: true, status: 'active',
    capabilities: { input: ['text'], output: ['text'], tools: true },
    cost: [{ input: 0, output: 0, cache: { read: 0, write: 0 } }],
    limit: { context: 200000, output: 32000 } });
  const cleanup = await plugin.setup({ app: { version: 'test' },
    model: { list: async () => [info('deepseek-v4.1-flash-free'), info('hy4-flash-free')] },
    session: { hook: async (name, fn) => { hooks.set(name, fn); return { dispose: async () => {} }; } } });
  const event = { sessionID: 'integration', agent: 'build', kind: 'primary',
    model: { providerID: 'opencode', id: 'deepseek-v4.1-flash-free' }, headers: { 'x-opencode-session-id': 'integration' } };
  await hooks.get('model.request')(event);
  const failed = { ...event, attempt: 2, error: { type: 'provider.free-limit', status: 429 }, decision: { retry: false } };
  await hooks.get('retry')(failed);
  assert.deepEqual(failed.decision, { retry: true, delay: 1000 });
  await hooks.get('experimental.model.resolve')(event);
  assert.equal(event.model.id, 'hy4-flash-free');
  assert.equal(event.headers['x-opencode-session-id'], 'integration');
  await cleanup();
});
