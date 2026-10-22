import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createPlanEditor } from '../src/editing/planEditor.js';

const examples = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../docs/migration-to-cloud/examples');
const example = name => JSON.parse(fs.readFileSync(path.join(examples, name), 'utf8'));
const json = (body, status = 200, headers = {}) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json', ...headers } });

function mockFetch(handler) {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    const call = { url: new URL(input), method: init.method, headers: init.headers, body: init.body ? JSON.parse(init.body) : undefined };
    calls.push(call);
    return handler(call, calls.length);
  };
  return { calls, restore: () => { globalThis.fetch = original; } };
}

test('capabilities only reports true for an explicit can_edit_plans', async () => {
  const mock = mockFetch(() => json({ can_edit_plans: true }));
  try {
    assert.deepEqual(await createPlanEditor().getCapabilities(), { canEditPlans: true, canEditShoppingTrips: false, canPublish: false, authenticated: false, username: null });
    assert.equal(mock.calls[0].url.pathname, '/api/v1/capabilities');
  } finally { mock.restore(); }
  const off = mockFetch(() => json({ can_edit_plans: 'yes' }));
  try { assert.equal((await createPlanEditor().getCapabilities()).canEditPlans, false); } finally { off.restore(); }
});

test('createPlan and propose send the documented bodies', async () => {
  const proposal = example('proposal.json');
  const mock = mockFetch(call => call.url.pathname.endsWith('/proposals') ? json(proposal, 201) : json(example('plan.json'), 201));
  try {
    const editor = createPlanEditor();
    await editor.createPlan('2026-11-02');
    assert.deepEqual(mock.calls[0].body, { week_start: '2026-11-02' });
    const result = await editor.propose(proposal.plan_id, 1);
    assert.deepEqual(mock.calls[1].body, { expected_revision: 1 });
    assert.equal(result.proposal_id, proposal.proposal_id);
    assert.equal(mock.calls[1].headers['Idempotency-Key'], undefined);
  } finally { mock.restore(); }
});

test('accept reuses its Idempotency-Key when retrying after an unknown outcome, then rotates', async () => {
  const result = example('mutation-result.json');
  const mock = mockFetch((call, n) => {
    if (n === 1) throw new TypeError('network down');
    if (n === 2) return json(example('error-generation-busy.json'), 503, { 'Retry-After': '7' });
    return json(result);
  });
  try {
    const editor = createPlanEditor();
    await assert.rejects(editor.acceptProposal(result.plan_id, 'prop_1', 1), { code: 'source_unavailable' });
    await assert.rejects(editor.acceptProposal(result.plan_id, 'prop_1', 1), err => err.code === 'generation_busy' && err.retryAfter === 7 && err.status === 503);
    assert.deepEqual(await editor.acceptProposal(result.plan_id, 'prop_1', 1), result);
    const keys = mock.calls.map(call => call.headers['Idempotency-Key']);
    assert.ok(keys[0] && keys[0] === keys[1] && keys[1] === keys[2]);
    await editor.acceptProposal(result.plan_id, 'prop_1', 1);
    assert.notEqual(mock.calls[3].headers['Idempotency-Key'], keys[0]);
    assert.deepEqual(mock.calls[0].body, { expected_revision: 1 });
  } finally { mock.restore(); }
});

test('a definitive error ends the action so the next attempt gets a fresh key', async () => {
  const mock = mockFetch(() => json(example('error-stale-revision.json'), 409));
  try {
    const editor = createPlanEditor();
    await assert.rejects(editor.undo('plan_2026_10_19', 2), err => err.code === 'stale_revision' && err.status === 409);
    await assert.rejects(editor.undo('plan_2026_10_19', 2), { code: 'stale_revision' });
    assert.notEqual(mock.calls[0].headers['Idempotency-Key'], mock.calls[1].headers['Idempotency-Key']);
    assert.equal(mock.calls[0].url.pathname, '/api/v1/plans/plan_2026_10_19/undo');
  } finally { mock.restore(); }
});

test('rule violations keep their details and proposal errors keep their codes', async () => {
  const mock = mockFetch(call => call.method === 'GET' ? json(example('error-proposal-expired.json'), 410) : json(example('error-rule-violation.json'), 422));
  try {
    const editor = createPlanEditor();
    await assert.rejects(editor.propose('plan_2026_10_19', 1), err => err.code === 'rule_violation' && err.details.length === 2 && err.details[0].field === 'weekly_protein_count');
    await assert.rejects(editor.getProposal('plan_2026_10_19', 'p1'), { code: 'proposal_expired' });
  } finally { mock.restore(); }
});

test('non-JSON proxy errors become source_unavailable and malformed error bodies source_invalid', async () => {
  let mode = 'html';
  const mock = mockFetch(() => mode === 'html' ? new Response('<html>Bad Gateway</html>', { status: 502 }) : json({ nope: true }, 500));
  try {
    const editor = createPlanEditor();
    await assert.rejects(editor.fillMonth('2026-11'), { code: 'source_unavailable' });
    mode = 'json';
    await assert.rejects(editor.fillMonth('2026-11'), { code: 'source_invalid' });
  } finally { mock.restore(); }
});

test('fillMonth, revisions, and discard use the documented endpoints', async () => {
  const fill = example('month-fill-result-partial.json');
  const revisions = example('revisions.json');
  const mock = mockFetch(call => call.url.pathname.endsWith('/fill') ? json(fill) : call.url.pathname.endsWith('/revisions') ? json(revisions) : json({}, 200));
  try {
    const editor = createPlanEditor();
    assert.equal((await editor.fillMonth('2026-11')).weeks.length, 6);
    assert.deepEqual(mock.calls[0].body, {});
    assert.equal((await editor.listRevisions(revisions.plan_id)).items.length, revisions.items.length);
    await editor.discardProposal(revisions.plan_id, 'p1');
    assert.equal(mock.calls[2].method, 'DELETE');
    await assert.rejects(editor.fillMonth('2026-13'), { code: 'invalid_request' });
  } finally { mock.restore(); }
});
