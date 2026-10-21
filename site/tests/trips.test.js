import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createApiReader } from '../src/readers/api.js';
import { createStaticReader } from '../src/readers/static.js';
import { deriveCoverage, validateTrip } from '../src/readers/shared.js';
import { createPlanEditor } from '../src/editing/planEditor.js';
import { runTripAction } from '../src/editing/tripActions.js';
import { countChecked, groupItems, itemCheckKey, readTripChecks, sortTrips, tripStorageKey, writeTripChecks } from '../src/tripModel.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const examples = path.resolve(here, '../../docs/migration-to-cloud/examples');
const staticDir = path.resolve(here, '../../docs/migration-to-cloud/static-examples/populated');
const readJson = file => JSON.parse(fs.readFileSync(file, 'utf8'));
const example = name => readJson(path.join(examples, name));
const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
const clone = value => structuredClone(value);

// ---------- static fixture built in the test: the populated publication plus trips ----------
const index = readJson(path.join(staticDir, 'index.json'));
const manifestFile = path.join(staticDir, index.manifest_path);
const baseManifest = readJson(manifestFile);
const planDocs = new Map(baseManifest.plans.flatMap(entry => [
  [entry.plan_path, readJson(path.join(path.dirname(manifestFile), entry.plan_path))],
  [entry.shopping_path, readJson(path.join(path.dirname(manifestFile), entry.shopping_path))],
]));
const [first, second] = baseManifest.plans.map(entry => entry.summary);

function tripFor(id, summaries, overrides = {}) {
  return {
    trip_id: id, name: `List ${id}`, status: 'open', version: 2, created_at: '2026-10-21T10:00:00-05:00', updated_at: '2026-10-21T10:05:00-05:00', completed_at: null,
    weeks: summaries.map(s => ({ plan_id: s.plan_id, week_start: s.week_start, week_end: s.week_end, revision: s.revision, snapshot_id: s.snapshot_id, current_revision: s.revision, coverage: 'on_list', changed_since_shopping: false })),
    items: [
      { ingredient_id: 'chicken', display: 'Pollo', quantity: 1080, unit: 'g', section: 'protein', plan_ids: summaries.map(s => s.plan_id) },
      { ingredient_id: 'milk', display: 'Leche', quantity: 2, unit: 'units', section: 'dairy', plan_ids: [summaries[0].plan_id] },
    ],
    ...overrides,
  };
}
function doneTrip(id, summaries, changed = false) {
  const trip = tripFor(id, summaries, { status: 'done', completed_at: '2026-10-21T11:00:00-05:00' });
  trip.weeks.forEach(w => { w.coverage = 'covered'; if (changed) { w.current_revision = w.revision + 1; w.changed_since_shopping = true; } });
  return trip;
}

function installBundle({ trips = [], manifestTrips, overrides = {} } = {}) {
  const original = globalThis.fetch;
  globalThis.window = { location: { origin: 'http://meal.test' } };
  const base = 'http://meal.test/meal-prep/publication/';
  const manifestUrl = `${base}${index.manifest_path}`;
  const dir = manifestUrl.slice(0, manifestUrl.lastIndexOf('/') + 1);
  const docs = new Map();
  docs.set(`${base}index.json`, index);
  docs.set(manifestUrl, { ...baseManifest, shopping_trips: manifestTrips ?? trips.map(t => ({ trip_id: t.trip_id, path: `shopping-trips/${t.trip_id}.json` })) });
  for (const [rel, doc] of planDocs) docs.set(`${dir}${rel}`, doc);
  for (const trip of trips) docs.set(`${dir}shopping-trips/${trip.trip_id}.json`, trip);
  for (const [key, value] of Object.entries(overrides)) docs.set(`${dir}${key}`, value);
  globalThis.fetch = async input => { const url = new URL(input); url.search = ''; const doc = docs.get(url.href); return doc === undefined ? new Response('', { status: 404 }) : json(doc); };
  return () => { globalThis.fetch = original; delete globalThis.window; };
}
const staticReader = () => createStaticReader('publication/index.json', '/meal-prep/');

test('example trips from the API contract pass strict validation', () => {
  for (const name of ['shopping-trip.json', 'shopping-trip-done.json']) validateTrip(example(name));
  for (const trip of example('shopping-trips.json').items) validateTrip(trip);
});

test('validateTrip rejects broken coverage, flags, weeks and items', () => {
  const base = example('shopping-trip.json');
  const mutations = [
    t => { t.status = 'done'; }, // done without completed_at/coverage
    t => { t.weeks[0].coverage = 'covered'; },
    t => { t.weeks[0].changed_since_shopping = true; },
    t => { t.weeks[1].week_start = t.weeks[0].week_start; },
    t => { t.weeks = []; },
    t => { t.items[0].plan_ids = ['plan_unknown']; },
    t => { t.items[0].unit = 'kg'; },
    t => { t.items.push(clone(t.items[0])); },
    t => { t.extra = 1; },
    t => { t.name = ''; },
    t => { t.weeks[0].current_revision = 5; }, // open trip must use the current revision
  ];
  for (const mutate of mutations) { const trip = clone(base); mutate(trip); assert.throws(() => validateTrip(trip), error => error.code === 'source_invalid'); }
});

test('API reader lists, gets and validates trips and maps trip_not_found', async () => {
  const original = globalThis.fetch;
  const trip = example('shopping-trip.json');
  try {
    globalThis.fetch = async input => { const url = new URL(input); return url.pathname.endsWith('/shopping-trips') ? json({ items: [trip] }) : url.pathname.endsWith(trip.trip_id) ? json(trip) : json({ error: { code: 'trip_not_found', message: 'No such list', details: [] } }, 404); };
    const reader = createApiReader();
    assert.deepEqual((await reader.listShoppingTrips()).items, [trip]);
    assert.deepEqual(await reader.getShoppingTrip(trip.trip_id), trip);
    await assert.rejects(reader.getShoppingTrip('trip_other'), error => error.code === 'trip_not_found');
    await assert.rejects(reader.getShoppingTrip('../x'), error => error.code === 'invalid_request');
    globalThis.fetch = async () => json({ items: [trip, trip] });
    await assert.rejects(reader.listShoppingTrips(), error => error.code === 'source_invalid');
    globalThis.fetch = async () => json({ ...trip, trip_id: 'trip_wrong' });
    await assert.rejects(reader.getShoppingTrip(trip.trip_id), error => error.code === 'source_invalid');
  } finally { globalThis.fetch = original; }
});

test('API reader coverage reads /coverage for the range and validates it', async () => {
  const original = globalThis.fetch;
  const coverage = example('coverage.json');
  const calls = [];
  try {
    globalThis.fetch = async input => { calls.push(new URL(input)); return json(coverage); };
    const reader = createApiReader();
    const result = await reader.getCoverage({ from: '2026-10-01', to: '2026-11-30' });
    assert.equal(calls[0].pathname, '/api/v1/coverage');
    assert.equal(calls[0].searchParams.get('from'), '2026-10-01');
    assert.deepEqual(result.items.map(i => i.coverage), ['covered', 'on_list', 'not_covered']);
    await assert.rejects(reader.getCoverage({ from: '2026-12-01', to: '2026-11-01' }), error => error.code === 'invalid_request');
    await assert.rejects(reader.getCoverage({ from: '2026-10-19', to: '2026-10-25' }), error => error.code === 'source_invalid', 'items outside the range');
    globalThis.fetch = async () => json({ items: [{ ...coverage.items[2], trip_id: 'trip_x' }] });
    await assert.rejects(reader.getCoverage({ from: '2026-11-01', to: '2026-11-30' }), error => error.code === 'source_invalid');
  } finally { globalThis.fetch = original; }
});

test('static reader serves published trips and derives coverage from them', async () => {
  const open = tripFor('trip_open', [first]);
  const done = doneTrip('trip_done', [second], true);
  const restore = installBundle({ trips: [open, done] });
  try {
    const reader = staticReader();
    assert.deepEqual((await reader.listShoppingTrips()).items.map(t => t.trip_id), ['trip_open', 'trip_done']);
    assert.deepEqual(await reader.getShoppingTrip('trip_open'), open);
    await assert.rejects(reader.getShoppingTrip('trip_nope'), error => error.code === 'trip_not_found');
    const coverage = await reader.getCoverage({ from: first.week_start, to: second.week_end });
    assert.deepEqual(coverage.items.map(c => [c.plan_id, c.coverage, c.trip_id, c.changed_since_shopping]), [
      [first.plan_id, 'on_list', 'trip_open', false],
      [second.plan_id, 'covered', 'trip_done', true],
    ]);
    assert.equal(coverage.items.length, 2);
    assert.deepEqual((await reader.getCoverage({ from: first.week_start, to: first.week_end })).items.length, 1);
  } finally { restore(); }
});

test('static reader without published trips reports an empty list and not_covered weeks', async () => {
  const restore = installBundle();
  try {
    const reader = staticReader();
    assert.deepEqual(await reader.listShoppingTrips(), { items: [] });
    assert.ok((await reader.getCoverage({ from: first.week_start, to: first.week_end })).items.every(c => c.coverage === 'not_covered' && c.trip_id === null));
  } finally { restore(); }
});

test('static reader requires shopping_trips in the manifest', async () => {
  const restore = installBundle();
  const original = globalThis.fetch;
  globalThis.fetch = async input => {
    const response = await original(input);
    if (!String(input).endsWith('manifest.json')) return response;
    const manifest = await response.json(); delete manifest.shopping_trips; return json(manifest);
  };
  try { await assert.rejects(staticReader().listShoppingTrips(), error => error.code === 'source_invalid'); } finally { restore(); }
});

test('static reader rejects incoherent or unsafe trips', async () => {
  const cases = {
    'open trip with a different revision than the published plan': tripFor('trip_a', [{ ...first, revision: first.revision + 1 }]),
    'open trip with a different snapshot': tripFor('trip_a', [{ ...first, snapshot_id: 'sha256:other' }]),
    'trip covering an unpublished plan': tripFor('trip_a', [{ ...first, plan_id: 'plan_unpublished' }]),
    'trip with a different week than the plan': tripFor('trip_a', [{ ...first, week_start: '2026-10-12', week_end: '2026-10-18' }]),
    'trip file with a different identity': tripFor('trip_b', [first]),
  };
  for (const [label, trip] of Object.entries(cases)) {
    const restore = installBundle({ trips: [trip], manifestTrips: [{ trip_id: 'trip_a', path: 'shopping-trips/trip_a.json' }], overrides: { 'shopping-trips/trip_a.json': trip } });
    try { await assert.rejects(staticReader().listShoppingTrips(), error => error.code === 'source_invalid', label); } finally { restore(); }
  }
  // A week on two trips violates the one-trip-per-week invariant.
  const restore = installBundle({ trips: [tripFor('trip_a', [first]), tripFor('trip_b', [first])] });
  try { await assert.rejects(staticReader().listShoppingTrips(), error => error.code === 'source_invalid'); } finally { restore(); }
});

test('static reader enforces trip path safety and duplicate manifest entries', async () => {
  const trip = tripFor('trip_a', [first]);
  for (const entry of [
    { trip_id: 'trip_a', path: '../trip_a.json' },
    { trip_id: 'trip_a', path: '/shopping-trips/trip_a.json' },
    { trip_id: 'trip_a', path: 'shopping-trips/../trip_a.json' },
    { trip_id: 'trip_a', path: 'plans/trip_a.json' },
    { trip_id: 'trip_a', path: 'shopping-trips/other.json' },
    { trip_id: 'trip_a', path: 'https://evil.test/shopping-trips/trip_a.json' },
    { trip_id: '../trip_a', path: 'shopping-trips/../trip_a.json' },
    { trip_id: 'trip_a', path: 'shopping-trips/trip_a.json', extra: true },
  ]) {
    const restore = installBundle({ trips: [trip], manifestTrips: [entry] });
    try { await assert.rejects(staticReader().listShoppingTrips(), error => error.code === 'source_invalid', JSON.stringify(entry)); } finally { restore(); }
  }
  const entry = { trip_id: 'trip_a', path: 'shopping-trips/trip_a.json' };
  const restore = installBundle({ trips: [trip], manifestTrips: [entry, entry] });
  try { await assert.rejects(staticReader().listShoppingTrips(), error => error.code === 'source_invalid'); } finally { restore(); }
});

test('static trips stay pinned until refresh and a broken replacement keeps the pin', async () => {
  const trip = tripFor('trip_a', [first]);
  let restore = installBundle({ trips: [trip] });
  const reader = staticReader();
  try {
    assert.equal((await reader.listShoppingTrips()).items.length, 1);
    restore();
    restore = installBundle();
    assert.equal((await reader.listShoppingTrips()).items.length, 1, 'ordinary reads stay pinned');
    restore();
    restore = installBundle({ trips: [{ ...trip, items: [] }], overrides: { 'shopping-trips/trip_a.json': { ...trip, version: 0 } } });
    await assert.rejects(reader.refresh(), error => error.code === 'source_invalid');
    assert.equal((await reader.getShoppingTrip('trip_a')).version, trip.version);
    restore();
    restore = installBundle();
    await reader.refresh();
    assert.equal((await reader.listShoppingTrips()).items.length, 0);
  } finally { restore(); }
});

test('deriveCoverage marks unlisted weeks not_covered and keeps changed_since_shopping only on done trips', () => {
  const result = deriveCoverage([first, second], [doneTrip('trip_d', [first], true)], { from: first.week_start, to: second.week_end });
  assert.deepEqual(result.items.map(c => [c.coverage, c.changed_since_shopping]), [['covered', true], ['not_covered', false]]);
});

// ---------- write calls ----------
function mockFetch(handler) {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (input, init = {}) => { const call = { url: new URL(input), method: init.method, headers: init.headers, body: init.body ? JSON.parse(init.body) : undefined }; calls.push(call); return handler(call); };
  return { calls, restore: () => { globalThis.fetch = original; } };
}

test('capabilities report can_edit_shopping_trips independently', async () => {
  const mock = mockFetch(() => json({ can_edit_plans: true, can_edit_shopping_trips: true }));
  try { assert.deepEqual(await createPlanEditor().getCapabilities(), { canEditPlans: true, canEditShoppingTrips: true, canPublish: false }); } finally { mock.restore(); }
});

test('trip writes send the documented requests with expected_version', async () => {
  const trip = example('shopping-trip.json');
  const mock = mockFetch(call => call.method === 'DELETE' && call.url.pathname === `/api/v1/shopping-trips/${trip.trip_id}` ? new Response(null, { status: 204 }) : json(trip, call.method === 'POST' && call.url.pathname.endsWith('/shopping-trips') ? 201 : 200));
  try {
    const editor = createPlanEditor();
    await editor.createTrip(['plan_2026_10_19', 'plan_2026_10_26'], '  My list ');
    assert.deepEqual(mock.calls[0].body, { plan_ids: ['plan_2026_10_19', 'plan_2026_10_26'], name: 'My list' });
    await editor.createTrip(['plan_2026_10_19']);
    assert.deepEqual(mock.calls[1].body, { plan_ids: ['plan_2026_10_19'] });
    await editor.addTripWeek(trip.trip_id, 'plan_2026_11_02', 2);
    assert.deepEqual([mock.calls[2].method, mock.calls[2].url.pathname, mock.calls[2].body], ['POST', `/api/v1/shopping-trips/${trip.trip_id}/weeks`, { plan_id: 'plan_2026_11_02', expected_version: 2 }]);
    await editor.removeTripWeek(trip.trip_id, 'plan_2026_10_26', 2);
    assert.deepEqual([mock.calls[3].method, mock.calls[3].url.pathname, mock.calls[3].url.searchParams.get('expected_version')], ['DELETE', `/api/v1/shopping-trips/${trip.trip_id}/weeks/plan_2026_10_26`, '2']);
    await editor.markTripDone(trip.trip_id, 2);
    assert.deepEqual([mock.calls[4].url.pathname, mock.calls[4].body], [`/api/v1/shopping-trips/${trip.trip_id}/done`, { expected_version: 2 }]);
    await editor.reopenTrip(trip.trip_id, 3);
    assert.deepEqual([mock.calls[5].url.pathname, mock.calls[5].body], [`/api/v1/shopping-trips/${trip.trip_id}/reopen`, { expected_version: 3 }]);
    assert.equal(await editor.deleteTrip(trip.trip_id, 3), undefined);
    assert.equal(mock.calls[6].url.searchParams.get('expected_version'), '3');
    await assert.rejects(editor.markTripDone(trip.trip_id, 0), error => error.code === 'invalid_request');
    await assert.rejects(editor.createTrip([]), error => error.code === 'invalid_request');
    assert.equal(mock.calls.length, 7);
  } finally { mock.restore(); }
});

test('stale_version refetches the trip and explains; other conflicts map to clear messages', async () => {
  const trip = example('shopping-trip.json');
  const fresh = { ...trip, version: trip.version + 1 };
  const mock = mockFetch(call => (call.method ?? 'GET') === 'GET' ? json(fresh) : json(example('error-stale-version.json'), 409));
  try {
    const editor = createPlanEditor();
    const reader = createApiReader();
    const outcome = await runTripAction(() => editor.markTripDone(trip.trip_id, trip.version), { reader, tripId: trip.trip_id });
    assert.match(outcome.message, /changed since you opened it/);
    assert.equal(outcome.trip.version, fresh.version);
    assert.equal(mock.calls.at(-1).method ?? 'GET', 'GET');
  } finally { mock.restore(); }
  const conflict = mockFetch(() => json(example('error-week-already-on-trip.json'), 409));
  try {
    const outcome = await runTripAction(() => createPlanEditor().createTrip(['plan_2026_10_26']), { reader: createApiReader() });
    assert.match(outcome.message, /already on a shopping list/);
    assert.equal(outcome.code, 'week_already_on_trip');
    assert.equal(outcome.trip, undefined);
    assert.equal(conflict.calls.length, 1, 'no refetch for week_already_on_trip');
  } finally { conflict.restore(); }
  const gone = mockFetch(() => json(example('error-trip-not-found.json'), 404));
  try { assert.equal((await runTripAction(() => createPlanEditor().deleteTrip('trip_x', 1), { reader: createApiReader(), tripId: 'trip_x' })).gone, true); } finally { gone.restore(); }
  const ok = await runTripAction(async () => null, { reader: null });
  assert.equal(ok.deleted, true);
});

// ---------- device-local item checkmarks ----------
function fakeStorage() {
  const data = new Map();
  return { getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value), data };
}

test('item checkmark keys are scoped by mode, trip, version, ingredient and unit', () => {
  const trip = example('shopping-trip.json');
  assert.notEqual(tripStorageKey('api', trip), tripStorageKey('static', trip));
  assert.notEqual(tripStorageKey('api', trip), tripStorageKey('api', { ...trip, version: trip.version + 1 }));
  assert.notEqual(tripStorageKey('api', trip), tripStorageKey('api', { ...trip, trip_id: 'trip_other' }));
  assert.match(tripStorageKey('api', trip), new RegExp(`${trip.trip_id}`));
  assert.notEqual(itemCheckKey({ ingredient_id: 'milk', unit: 'ml' }), itemCheckKey({ ingredient_id: 'milk', unit: 'units' }));
});

test('checkmarks persist per scope, tolerate bad or unavailable storage, and never touch coverage', () => {
  const storage = fakeStorage();
  globalThis.window = { localStorage: storage };
  try {
    const trip = example('shopping-trip.json');
    const key = tripStorageKey('static', trip);
    assert.deepEqual(readTripChecks(key), {});
    writeTripChecks(key, { 'chicken|g': true });
    assert.deepEqual(readTripChecks(key), { 'chicken|g': true });
    assert.deepEqual(readTripChecks(tripStorageKey('static', { ...trip, version: 99 })), {}, 'a new version starts clean');
    storage.setItem(key, '[1,2]'); assert.deepEqual(readTripChecks(key), {});
    storage.setItem(key, '{broken'); assert.deepEqual(readTripChecks(key), {});
    globalThis.window = { localStorage: { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } } };
    assert.deepEqual(readTripChecks(key), {});
    assert.doesNotThrow(() => writeTripChecks(key, { a: true }));
  } finally { delete globalThis.window; }
});

test('groupItems keeps section order, moves checked items down and counts them', () => {
  const items = [
    { ingredient_id: 'a', unit: 'g', section: 'protein' }, { ingredient_id: 'b', unit: 'g', section: 'protein' },
    { ingredient_id: 'c', unit: 'units', section: 'dairy' }, { ingredient_id: 'c', unit: 'g', section: 'dairy' },
  ];
  const checks = { 'a|g': true };
  const groups = groupItems(items, checks);
  assert.deepEqual(groups.map(g => g.section), ['protein', 'dairy']);
  assert.deepEqual(groups[0].items.map(i => i.ingredient_id), ['b', 'a']);
  assert.deepEqual([groups[0].checked, groups[0].total], [1, 2]);
  assert.equal(countChecked(items, checks), 1);
  assert.equal(countChecked(items, { 'c|g': true, 'c|units': true }), 2);
});

test('sortTrips lists open trips first by soonest week, then done trips newest first', () => {
  const mk = (id, status, start) => ({ trip_id: id, status, weeks: [{ week_start: start }] });
  assert.deepEqual(sortTrips([mk('d1', 'done', '2026-09-01'), mk('o2', 'open', '2026-11-02'), mk('d2', 'done', '2026-10-01'), mk('o1', 'open', '2026-10-19')]).map(t => t.trip_id), ['o1', 'o2', 'd2', 'd1']);
});
