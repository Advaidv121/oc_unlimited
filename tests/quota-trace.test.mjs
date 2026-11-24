import { test } from 'node:test';
import assert from 'node:assert/strict';
import plugin, { contextMetadata } from '../plugins/quota-trace/index.js';

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
  assert.deepEqual([...hooks.keys()], ['context', 'model.request', 'http.request', 'http.response', 'retry']);
  const event = { error: { type: 'provider.free-limit', status: 429 }, decision: { retry: false } };
  hooks.get('retry')(event);
  assert.deepEqual(event.decision, { retry: false });
  await cleanup();
  assert.equal(disposed, 5);
});
