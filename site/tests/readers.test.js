import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createApiReader } from '../src/readers/api.js';
import { createStaticReader } from '../src/readers/static.js';

const siteDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const fixtureDir = path.resolve(siteDir, '../docs/migration-to-cloud/static-examples');
// Until the shared fixtures publish shopping_trips, inject an empty list so these tests exercise the strict manifest.
const withTrips = (file, text) => { if (!file.endsWith('manifest.json')) return text; const m = JSON.parse(text); return JSON.stringify({ shopping_trips: [], ...m }); };
const readJson = file => JSON.parse(fs.readFileSync(file, 'utf8'));
const populated = path.join(fixtureDir, 'populated');
const index = readJson(path.join(populated, 'index.json'));
const manifest = readJson(path.join(populated, index.manifest_path));
const entry = manifest.plans[0];
const plan = readJson(path.join(path.dirname(path.join(populated,index.manifest_path)),entry.plan_path));
const shopping = readJson(path.join(path.dirname(path.join(populated,index.manifest_path)),entry.shopping_path));

function installStaticFetch(bundle = populated) {
  const original = globalThis.fetch;
  globalThis.window = { location: { origin: 'http://meal.test' } };
  globalThis.fetch = async input => {
    const url = new URL(input);
    const root = path.join(bundle);
    const relative = url.pathname.replace('/meal-prep/publication/','');
    const file = path.resolve(root,relative);
    if (!file.startsWith(`${path.resolve(root)}${path.sep}`)) return new Response('',{status:400});
    if (!fs.existsSync(file)) return new Response('',{status:404});
    return new Response(withTrips(file,fs.readFileSync(file,'utf8')),{status:200,headers:{'content-type':'application/json'}});
  };
  return () => { globalThis.fetch = original; delete globalThis.window; };
}

test('API and static readers return the same normalized DTOs from the contract fixture', async () => {
  const restoreStatic = installStaticFetch();
  const staticReader = createStaticReader('publication/index.json','/meal-prep/');
  const original = globalThis.fetch;
  const range = {from:'2026-10-19',to:'2026-10-25'};
  const staticPlans = await staticReader.listPlans(range);
  const staticPlan = await staticReader.getPlan(plan.plan_id);
  const staticShopping = await staticReader.getShoppingList(plan.plan_id,plan.revision,plan.snapshot_id);
  globalThis.fetch = async input => {
    const url = new URL(input);
    const body = url.pathname.endsWith('/shopping-list') ? shopping : url.pathname.endsWith('/plans') ? {items:manifest.plans.map(p=>p.summary).filter(p=>p.week_start<='2026-10-25'&&p.week_end>='2026-10-19')} : plan;
    return new Response(JSON.stringify(body),{status:200,headers:{'content-type':'application/json'}});
  };
  try {
    const apiReader = createApiReader();
    assert.deepEqual(staticPlans, await apiReader.listPlans(range));
    assert.deepEqual(staticPlan, await apiReader.getPlan(plan.plan_id));
    assert.deepEqual(staticShopping, await apiReader.getShoppingList(plan.plan_id,plan.revision,plan.snapshot_id));
    assert.equal(staticReader.canEditPlans,false); assert.equal(apiReader.canEditPlans,false);
  } finally { globalThis.fetch = original; restoreStatic(); }
});

test('date ranges reject invalid dates, reversed dates, and ranges over 366 days', async () => {
  const restore = installStaticFetch();
  try {
    const reader = createStaticReader('publication/index.json','/meal-prep/');
    for (const range of [{from:'2026-02-30',to:'2026-03-01'},{from:'2026-10-20',to:'2026-10-19'},{from:'2025-01-01',to:'2026-01-02'}])
      await assert.rejects(reader.listPlans(range), error => error.code === 'invalid_request');
  } finally { restore(); }
});

test('static reader reports an empty publication and does not synthesize a plan', async () => {
  const restore = installStaticFetch(path.join(fixtureDir,'empty'));
  try {
    const reader = createStaticReader('publication/index.json','/meal-prep/');
    assert.deepEqual(await reader.listPlans({from:'2026-12-01',to:'2026-12-31'}),{items:[]});
    assert.equal(reader.context.publication_id,'pub_empty_01');
  } finally { restore(); }
});

test('static refresh pins one complete publication and keeps the prior pin on a broken replacement', async () => {
  let activeBundle = populated;
  const original = globalThis.fetch;
  globalThis.window = {location:{origin:'http://meal.test'}};
  globalThis.fetch = async input => {
    const url = new URL(input), root = path.resolve(activeBundle), relative = url.pathname.replace('/meal-prep/publication/',''), file = path.resolve(root,relative);
    if (!file.startsWith(`${root}${path.sep}`) || !fs.existsSync(file)) return new Response('',{status:404});
    return new Response(withTrips(file,fs.readFileSync(file,'utf8')),{status:200});
  };
  try {
    const reader = createStaticReader('publication/index.json','/meal-prep/');
    await reader.listPlans({from:'2026-10-19',to:'2026-10-25'});
    const previous = reader.context.publication_id;
    activeBundle = path.join(fixtureDir,'empty');
    await reader.listPlans({from:'2026-10-19',to:'2026-10-25'});
    assert.equal(reader.context.publication_id,previous,'ordinary reads remain pinned');
    await reader.refresh();
    assert.equal(reader.context.publication_id,'pub_empty_01');
    activeBundle = populated;
    await reader.refresh();
    const restoredId = reader.context.publication_id;
    // A malformed plan in a future bundle cannot replace the currently pinned publication.
    const malformed = {...plan, snapshot_id:'wrong'};
    globalThis.fetch = async input => {
      const url = new URL(input), root = path.resolve(populated), relative = url.pathname.replace('/meal-prep/publication/',''), file = path.resolve(root,relative);
      if (relative.endsWith('/plan.json')) return new Response(JSON.stringify(malformed),{status:200});
      if (!file.startsWith(`${root}${path.sep}`) || !fs.existsSync(file)) return new Response('',{status:404});
      return new Response(withTrips(file,fs.readFileSync(file,'utf8')),{status:200});
    };
    await assert.rejects(reader.refresh(), error => error.code === 'source_invalid');
    assert.equal(reader.context.publication_id,restoredId);
    const stillPinned = await reader.getPlan(plan.plan_id);
    assert.equal(stillPinned.snapshot_id,plan.snapshot_id);
    assert.equal((await reader.getShoppingList(plan.plan_id,plan.revision,plan.snapshot_id)).snapshot_id,plan.snapshot_id);
    await assert.rejects(reader.getShoppingList(plan.plan_id,plan.revision,'another-snapshot'),error=>error.code==='snapshot_changed');
  } finally { globalThis.fetch = original; delete globalThis.window; }
});

test('unsafe paths, mismatched DTOs, and missing files fail without API or legacy-file fallback', async () => {
  const original = globalThis.fetch;
  globalThis.window = {location:{origin:'http://meal.test'}};
  const documents = new Map();
  const baseUrl='http://meal.test/meal-prep/publication/';
  documents.set(`${baseUrl}index.json`,{schema_version:1,publication_id:'pub_test',manifest_path:'publications/pub_test/manifest.json'});
  const summary = Object.fromEntries(['plan_id','week_start','week_end','timezone','revision','snapshot_id','generated_at','seed'].map(key=>[key,plan[key]]));
  documents.set(`${baseUrl}publications/pub_test/manifest.json`,{schema_version:1,publication_id:'pub_test',exported_at:'2026-10-21T15:00:00Z',selected_months:['2026-10'],shopping_trips:[],timezone:'America/Bogota',plans:[{summary,plan_path:'../plan.json',shopping_path:'plans/plan_2026_10_19/shopping-list.json'}]});
  globalThis.fetch = async input => { const value=documents.get(String(input)); return value ? new Response(JSON.stringify(value),{status:200}) : new Response('',{status:404}); };
  try {
    const reader=createStaticReader('publication/index.json','/meal-prep/');
    await assert.rejects(reader.listPlans({from:'2026-10-19',to:'2026-10-25'}),error=>error.code==='source_invalid');
    const api=createApiReader();
    globalThis.fetch=async()=>new Response(JSON.stringify({error:{code:'snapshot_changed',message:'stale',details:[]}}),{status:409});
    await assert.rejects(api.getPlan(plan.plan_id),error=>error.code==='snapshot_changed');
    await assert.rejects(api.getShoppingList(plan.plan_id,plan.revision,plan.snapshot_id),error=>error.code==='snapshot_changed');
    globalThis.fetch=async()=>new Response('proxy error',{status:500,headers:{'Content-Type':'text/plain'}});
    await assert.rejects(api.getPlan(plan.plan_id),error=>error.code==='source_unavailable');
    globalThis.fetch=async()=>new Response('not json',{status:200});
    await assert.rejects(api.getPlan(plan.plan_id),error=>error.code==='source_invalid');
  } finally { globalThis.fetch=original; delete globalThis.window; }
});
