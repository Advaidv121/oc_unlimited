import { test } from 'node:test';
import assert from 'node:assert/strict';
import { SessionRecovery } from '../plugins/quota-trace/recovery.js';

const event = () => ({ sessionID: 'local', model: { providerID: 'opencode', id: 'deepseek-free' },
  error: { type: 'provider.free-limit', status: 429 }, attempt: 2, decision: { retry: false } });

test('one bounded replacement, acceptance, reuse, and success cooldown', () => {
  let now = 0;
  const recovery = new SessionRecovery(() => now, () => 'ses_replacement');
  const e = event();
  assert(recovery.begin(e));
  assert.deepEqual(e.decision, { retry: true, delay: 1000 });
  const request = { ...e, headers: { 'x-opencode-session-id': 'local', 'x-opencode-session': 'local' } };
  assert(recovery.apply(request));
  assert.equal(request.sessionID, 'local');
  assert.equal(request.headers['x-opencode-session-id'], 'ses_replacement');
  assert(!recovery.begin(e));
  assert.equal(recovery.response({ ...e, response: { status: 200 } }), true);
  assert(!recovery.begin(e));
  now = 30001;
  assert(recovery.begin(e));
});

test('failed replacement discarded and ten minute backoff', () => {
  let now = 0;
  const recovery = new SessionRecovery(() => now);
  const e = event();
  assert(recovery.begin(e));
  assert.equal(recovery.response({ ...e, response: { status: 429 } }), false);
  assert(!recovery.apply({ ...e, headers: {} }));
  assert(!recovery.begin(e));
  now = 600001;
  assert(recovery.begin(e));
});

test('parent headers cannot override replacement and models stay isolated', () => {
  const recovery = new SessionRecovery(); const e = event(); recovery.begin(e);
  const request = { ...e, headers: { 'x-opencode-parent-session-id': 'parent', 'x-parent-session-id': 'parent' } };
  recovery.apply(request);
  assert.equal(request.headers['x-opencode-parent-session-id'], request.headers['x-session-affinity']);
  assert.notEqual(request.headers['x-opencode-session-id'], request.headers['x-session-affinity']);
  assert(!recovery.apply({ ...e, model: { ...e.model, id: 'other-free' }, headers: {} }));
});

test('does not intercept paid models, unrelated failures, auxiliary requests or later attempts', () => {
  for (const patch of [{ model: { providerID: 'opencode', id: 'paid' } },
    { error: { type: 'provider.transport', status: 429 } }, { attempt: 3 }, { kind: 'title' }]) {
    const e = { ...event(), ...patch }; assert(!new SessionRecovery().begin(e));
    assert.deepEqual(e.decision, { retry: false });
  }
});
