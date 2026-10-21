import test from 'node:test';
import assert from 'node:assert/strict';
import { POLL_TIMEOUT_MS, canPublishNow, defaultMonths, isBusy, isTerminal, jobStatusText, lastPublishedText, monthChoices, monthsParam, pollDecision, previewSummary, publishErrorMessage, toggleMonth, weekMarker } from '../src/publishModel.js';
import { createPublishApi } from '../src/editing/publishApi.js';
import { createPlanEditor } from '../src/editing/planEditor.js';

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
const job = (status, extra = {}) => ({ job_id: 'j1', months: ['2026-10'], status, publication_id: null, commit_sha: null, run_url: null, error: null, started_at: '2026-10-21T10:00:00Z', finished_at: null, deployed_at: null, ...extra });

test('month range spans two back to four ahead, across year ends', () => {
  assert.deepEqual(monthChoices('2026-10'), ['2026-08', '2026-09', '2026-10', '2026-11', '2026-12', '2027-01', '2027-02']);
  assert.deepEqual(monthChoices('2026-01').slice(0, 2), ['2025-11', '2025-12']);
  assert.deepEqual(defaultMonths('2026-12'), ['2026-12', '2027-01']);
});

test('toggling keeps months sorted, valid and within twelve', () => {
  assert.deepEqual(toggleMonth(['2026-11'], '2026-10'), ['2026-10', '2026-11']);
  assert.deepEqual(toggleMonth(['2026-10', '2026-11'], '2026-10'), ['2026-11']);
  assert.deepEqual(toggleMonth(['2026-10'], 'nope'), ['2026-10']);
  const twelve = monthChoices('2026-01', 0, 11);
  assert.equal(toggleMonth(twelve, '2027-01').length, 12);
  assert.equal(monthsParam(['2026-11', '2026-10']), '2026-10,2026-11');
});

test('week markers come from published_revision', () => {
  assert.equal(weekMarker({ revision: 1, published_revision: null }), 'new');
  assert.equal(weekMarker({ revision: 3, published_revision: 2 }), 'changed');
  assert.equal(weekMarker({ revision: 2, published_revision: 2 }), 'unchanged');
  const summary = previewSummary({ weeks: [{ revision: 1, published_revision: null }, { revision: 3, published_revision: 2 }, { revision: 2, published_revision: 2 }], trips: [{}] });
  assert.deepEqual(summary, { new: 1, changed: 1, unchanged: 1, weeks: 3, trips: 1 });
});

test('publish is enabled only with a changed, loaded preview and no job in flight', () => {
  const preview = { weeks: [{ revision: 1, published_revision: null }], trips: [], unchanged: false };
  const base = { selected: ['2026-10'], preview, previewing: false, publishing: false, job: null };
  assert.equal(canPublishNow(base), true);
  assert.equal(canPublishNow({ ...base, preview: { ...preview, unchanged: true } }), false);
  assert.equal(canPublishNow({ ...base, selected: [] }), false);
  assert.equal(canPublishNow({ ...base, preview: null }), false);
  assert.equal(canPublishNow({ ...base, previewing: true }), false);
  assert.equal(canPublishNow({ ...base, publishing: true }), false);
  assert.equal(canPublishNow({ ...base, job: job('pushed') }), false);
  assert.equal(canPublishNow({ ...base, job: job('live') }), true);
  assert.equal(canPublishNow({ ...base, preview: { ...preview, weeks: [] } }), false);
});

test('job status text and polling decisions', () => {
  assert.match(jobStatusText(job('pushed')), /Waiting/);
  assert.match(jobStatusText(job('live')), /^Live/);
  assert.match(jobStatusText(job('failed', { error: 'push rejected' })), /push rejected.*previous public version/);
  assert.match(jobStatusText(job('unchanged')), /Nothing changed/);
  assert.equal(isBusy(job('running')), true);
  assert.equal(isTerminal(job('failed')), true);
  assert.equal(pollDecision(job('pushed'), 1000), 'poll');
  assert.equal(pollDecision(job('pushed'), POLL_TIMEOUT_MS), 'timeout');
  assert.equal(pollDecision(job('live'), 1000), 'stop');
  assert.equal(pollDecision(job('failed'), POLL_TIMEOUT_MS + 1), 'stop');
});

test('error messages and last-published text', () => {
  assert.match(publishErrorMessage({ code: 'publish_in_progress' }), /another publish is running|Another publish is running/);
  assert.match(publishErrorMessage({ code: 'publish_failed', message: 'git push failed' }), /git push failed/);
  assert.equal(publishErrorMessage({ code: 'weird', message: 'Boom' }), 'Boom');
  assert.equal(lastPublishedText({ last_live_job: null }), 'Nothing has been published yet.');
  assert.equal(lastPublishedText({ last_live_job: job('live', { deployed_at: 'T' }) }, value => `at ${value}`), 'Last published at T: 2026-10.');
});

function mockFetch(handler) {
  const original = globalThis.fetch, calls = [];
  globalThis.fetch = async (url, init = {}) => { calls.push({ url: String(url), method: init.method, body: init.body }); return handler(String(url), init); };
  return { calls, restore: () => { globalThis.fetch = original; } };
}

test('publish client calls the contract endpoints and maps errors', async () => {
  const mock = mockFetch((url, init) => {
    if (url.endsWith('/publishing')) return json({ enabled: true, site_url: 'https://x.github.io/meal-plan/', latest_job: null, last_live_job: null });
    if (url.includes('/publishing/preview')) return json({ months: ['2026-10', '2026-11'], weeks: [], trips: [], unchanged: true });
    if (url.endsWith('/publishing/jobs') && init.method === 'POST') return json({ error: { code: 'publish_in_progress', message: 'busy', details: [] } }, 409);
    if (url.endsWith('/publishing/jobs/j1')) return json(job('live'));
    return json({}, 404);
  });
  try {
    const api = createPublishApi('/api/v1');
    assert.equal((await api.getStatus()).enabled, true);
    assert.equal((await api.preview(['2026-11', '2026-10'])).unchanged, true);
    assert.match(mock.calls[1].url, /\/api\/v1\/publishing\/preview\?months=2026-10%2C2026-11$/);
    await assert.rejects(api.publish(['2026-11', '2026-10']), error => error.code === 'publish_in_progress' && error.status === 409);
    assert.deepEqual(JSON.parse(mock.calls[2].body), { months: ['2026-10', '2026-11'] });
    assert.equal((await api.getJob('j1')).status, 'live');
  } finally { mock.restore(); }
});

test('capabilities expose can_publish', async () => {
  const mock = mockFetch(() => json({ can_edit_plans: true, can_edit_shopping_trips: true, can_publish: true }));
  try { assert.equal((await createPlanEditor().getCapabilities()).canPublish, true); } finally { mock.restore(); }
});
