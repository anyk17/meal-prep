import test from 'node:test';
import assert from 'node:assert/strict';
import { createPlanEditor } from '../src/editing/planEditor.js';
import { filterVariants, proteinOptions, canSwapSlot, groupGroceryDelta, formatDifference, tripImpactMessage, replacedSwapsWarning, swapErrorMessage, swapNeedsReload } from '../src/editing/swapModel.js';

const json = (body, status = 200, headers = {}) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json', ...headers } });
function mockFetch(handler) {
  const original = globalThis.fetch; const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    const call = { url: new URL(input), method: init.method, headers: init.headers, body: init.body ? JSON.parse(init.body) : undefined };
    calls.push(call); return handler(call, calls.length);
  };
  return { calls, restore: () => { globalThis.fetch = original; } };
}
const err = (code, status) => json({ error: { code, message: code, details: [] } }, status);
const swap = { slotId: 'slot_2026-10-21_lunch', variantId: 'pollo__carb_rice', scope: 'pair', expectedRevision: 2 };
const plan = { plan_id: 'plan_2026_10_19', revision: 3 };

test('getCatalogVariants returns items', async () => {
  const mock = mockFetch(() => json({ items: [{ recipe_id: 'a', variant_id: 'a__b', name: 'A', protein: 'fish', carb: null }] }));
  try {
    const items = await createPlanEditor().getCatalogVariants();
    assert.equal(items.length, 1);
    assert.equal(mock.calls[0].url.pathname, '/api/v1/catalog/variants');
    assert.equal(mock.calls[0].method, 'GET');
  } finally { mock.restore(); }
  const bad = mockFetch(() => json({}));
  try { await assert.rejects(createPlanEditor().getCatalogVariants(), { code: 'source_invalid' }); } finally { bad.restore(); }
});

test('previewSwap posts the documented body without an idempotency key', async () => {
  const preview = { plan_id: plan.plan_id, base_revision: 2, changes: [], grocery_delta: [], rule_warnings: [], trip: null };
  const mock = mockFetch(() => json(preview));
  try {
    assert.deepEqual(await createPlanEditor().previewSwap(plan.plan_id, swap), preview);
    assert.equal(mock.calls[0].url.pathname, `/api/v1/plans/${plan.plan_id}/swaps/preview`);
    assert.deepEqual(mock.calls[0].body, { slot_id: swap.slotId, variant_id: swap.variantId, scope: 'pair', expected_revision: 2 });
    assert.equal(mock.calls[0].headers['Idempotency-Key'], undefined);
  } finally { mock.restore(); }
});

test('applySwap reuses its Idempotency-Key on retry, then rotates', async () => {
  const mock = mockFetch((call, n) => {
    if (n === 1) throw new TypeError('network down');
    if (n === 2) return err('unavailable', 503);
    return json({ plan, rule_warnings: [{ rule: 'r', message: 'm' }] }, 201);
  });
  try {
    const editor = createPlanEditor();
    await assert.rejects(editor.applySwap(plan.plan_id, swap), { code: 'source_unavailable' });
    await assert.rejects(editor.applySwap(plan.plan_id, swap));
    const result = await editor.applySwap(plan.plan_id, swap);
    assert.equal(mock.calls[2].body.scope, 'pair');
    const [k1, k2] = mock.calls.map(c => c.headers['Idempotency-Key']);
    assert.ok(k1); assert.equal(k1, k2);
    assert.deepEqual(result, { plan, ruleWarnings: [{ rule: 'r', message: 'm' }] });
    assert.equal(mock.calls[2].headers['Idempotency-Key'], k1);
    await editor.applySwap(plan.plan_id, swap);
    assert.notEqual(mock.calls[3].headers['Idempotency-Key'], k1);
  } finally { mock.restore(); }
});

test('applySwap treats a 200 no-op as success and maps errors', async () => {
  const noop = mockFetch(() => json({ plan, rule_warnings: [] }, 200));
  try { assert.equal((await createPlanEditor().applySwap(plan.plan_id, swap)).plan.revision, 3); } finally { noop.restore(); }
  for (const [code, status] of [['stale_revision', 409], ['slot_not_editable', 409], ['slot_not_found', 404], ['variant_not_found', 404], ['forbidden', 403]]) {
    const mock = mockFetch(() => err(code, status));
    try { await assert.rejects(createPlanEditor().applySwap(plan.plan_id, swap), error => error.code === code && error.status === status); } finally { mock.restore(); }
  }
});

test('swap calls validate input before fetching', async () => {
  const mock = mockFetch(() => json({}));
  try {
    const editor = createPlanEditor();
    await assert.rejects(editor.previewSwap(plan.plan_id, { ...swap, scope: 'both' }), { code: 'invalid_request' });
    await assert.rejects(editor.applySwap(plan.plan_id, { ...swap, expectedRevision: 0 }), { code: 'invalid_request' });
    assert.equal(mock.calls.length, 0);
  } finally { mock.restore(); }
});

test('variant search and protein filter', () => {
  const items = [
    { variant_id: 'a', name: 'Pollo toscano', protein: 'chicken', carb: 'rice' },
    { variant_id: 'b', name: 'Salmón al horno', protein: 'fish', carb: null },
    { variant_id: 'c', name: 'Pollo al curry', protein: 'chicken', carb: null },
  ];
  assert.deepEqual(filterVariants(items, 'salmon').map(i => i.variant_id), ['b']);
  assert.deepEqual(filterVariants(items, 'pollo cur').map(i => i.variant_id), ['c']);
  assert.deepEqual(filterVariants(items, '', 'chicken').map(i => i.variant_id), ['a', 'c']);
  assert.deepEqual(filterVariants(items, 'toscano', 'fish'), []);
  assert.deepEqual(proteinOptions(items), ['chicken', 'fish']);
});

test('only slots dated today or later can be swapped', () => {
  assert.equal(canSwapSlot({ date: '2026-10-21' }, '2026-10-21'), true);
  assert.equal(canSwapSlot({ date: '2026-10-20' }, '2026-10-21'), false);
});

test('grocery delta grouping and formatting', () => {
  const groups = groupGroceryDelta([
    { ingredient_id: 'x', display: 'Zeta', unit: 'g', section: 'veg', before: 10, after: 10, difference: 0 },
    { ingredient_id: 'a', display: 'Arroz', unit: 'g', section: 'pantry', before: 0, after: 70, difference: 70 },
    { ingredient_id: 'p', display: 'Pollo', unit: 'g', section: 'meat', before: 500, after: 240, difference: -260 },
    { ingredient_id: 'l', display: 'Limón', unit: 'units', section: 'meat', before: 1, after: 1.5, difference: 0.5 },
  ]);
  assert.deepEqual(groups.map(g => g.section), ['meat', 'pantry']);
  assert.deepEqual(groups[0].items.map(i => i.text), ['+0.5 units', '−260 g']);
  assert.equal(groups[0].items[1].direction, 'less');
  assert.equal(formatDifference(70, 'g'), '+70 g');
});

test('trip impact messages', () => {
  assert.equal(tripImpactMessage(null), null);
  assert.equal(tripImpactMessage({ name: 'Semana 1', status: 'open', extra_needed: [] }).text, "Shopping list 'Semana 1' will update automatically.");
  const done = tripImpactMessage({ name: 'Semana 1', status: 'done', extra_needed: [{ ingredient_id: 'a', display: 'Arroz', unit: 'g', quantity: 70 }, { ingredient_id: 'b', display: 'Limón', unit: 'units', quantity: 2 }] });
  assert.equal(done.text, "'Semana 1' is already done — you'll need: Arroz 70 g, Limón 2 units.");
});

test('replaced_swaps produces the regenerate warning', () => {
  assert.equal(replacedSwapsWarning({ replaced_swaps: 0 }), null);
  assert.equal(replacedSwapsWarning({}), null);
  assert.equal(replacedSwapsWarning({ replaced_swaps: 1 }), 'This will replace 1 meal you swapped.');
  assert.equal(replacedSwapsWarning({ replaced_swaps: 3 }), 'This will replace 3 meals you swapped.');
});

test('swap error messages', () => {
  assert.match(swapErrorMessage({ code: 'stale_revision' }), /changed/);
  assert.match(swapErrorMessage({ code: 'slot_not_editable' }), /past/);
  assert.equal(swapErrorMessage({ code: 'other', message: 'boom' }), 'boom');
  assert.equal(swapNeedsReload({ code: 'stale_revision' }), true);
  assert.equal(swapNeedsReload({ code: 'variant_not_found' }), false);
});
