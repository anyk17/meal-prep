import { ReaderError, validTimestamp, validatePair, validateRange, validateSummary } from './shared.js';

const idPattern = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;
function fail(message) { throw new ReaderError('source_invalid', message); }
function validTimezone(value) { try { new Intl.DateTimeFormat('en',{timeZone:value}); return true; } catch { return false; } }
export function createStaticReader(indexPath = 'publication/index.json', baseUrl = (import.meta.env?.BASE_URL) || '/') {
  const indexUrl = new URL(indexPath, new URL(baseUrl, window.location.origin));
  let pinned = null;
  let refreshNumber = 0;
  async function json(url, missingCode = 'source_invalid') {
    let response; try { response = await fetch(url, { cache: 'no-store' }); } catch { throw new ReaderError('source_unavailable','The publication could not be reached.'); }
    if (response.status === 404) throw new ReaderError(missingCode, missingCode === 'publication_expired' ? 'The pinned publication is no longer available. Refresh to select the current publication.' : 'A referenced publication file is missing.');
    if (!response.ok) throw new ReaderError('source_unavailable',`Publication request failed (${response.status}).`);
    try { return await response.json(); } catch { fail('Publication file contains invalid JSON'); }
  }
  function safeRelative(parentUrl, path, pattern) {
    if (typeof path !== 'string' || !pattern.test(path) || path.includes('..') || path.startsWith('/')) fail('Publication contains an unsafe path');
    const url = new URL(path, parentUrl);
    if (url.origin !== indexUrl.origin || !url.pathname.startsWith(indexUrl.pathname.slice(0, indexUrl.pathname.lastIndexOf('/') + 1))) fail('Publication path escapes its root');
    return url;
  }
  async function load(force = false) {
    if (pinned && !force) return pinned;
    const indexRequestUrl = new URL(indexUrl);
    if (force) indexRequestUrl.searchParams.set('_refresh', `${Date.now()}-${++refreshNumber}`);
    const index = await json(indexRequestUrl);
    if (!index || Object.keys(index).sort().join(',') !== 'manifest_path,publication_id,schema_version' || index.schema_version !== 1 || !idPattern.test(index.publication_id || '') || typeof index.manifest_path !== 'string' || index.manifest_path !== `publications/${index.publication_id}/manifest.json`) fail('Invalid publication index');
    const manifestUrl = safeRelative(indexUrl,index.manifest_path,/^publications\/[A-Za-z0-9][A-Za-z0-9_-]*\/manifest\.json$/);
    const manifest = await json(manifestUrl, pinned ? 'publication_expired' : 'source_invalid');
    const manifestKeys = 'exported_at,plans,publication_id,schema_version,selected_months,timezone';
    if (!manifest || Object.keys(manifest).sort().join(',') !== manifestKeys || manifest.schema_version !== 1 || manifest.publication_id !== index.publication_id || !Array.isArray(manifest.plans) || !Array.isArray(manifest.selected_months) || !manifest.selected_months.length || new Set(manifest.selected_months).size !== manifest.selected_months.length || manifest.selected_months.some((month,i) => typeof month !== 'string' || !/^\d{4}-(0[1-9]|1[0-2])$/.test(month) || month.startsWith('0000') || (i > 0 && manifest.selected_months[i-1] >= month)) || typeof manifest.timezone !== 'string' || !manifest.timezone || !validTimezone(manifest.timezone) || !validTimestamp(manifest.exported_at)) fail('Invalid publication manifest');
    const entries = new Map(), weeks = new Set();
    for (const entry of manifest.plans) {
      if (!entry || Object.keys(entry).sort().join(',') !== 'plan_path,shopping_path,summary') fail('Invalid publication plan entry');
      validateSummary(entry?.summary);
      const id = entry.summary.plan_id;
      if (entry.summary.timezone !== manifest.timezone) fail('Plan timezone does not match publication timezone');
      if (entries.has(id) || weeks.has(entry.summary.week_start)) fail('Duplicate plan in publication');
      if (entries.size && [...entries.values()].at(-1).summary.week_start >= entry.summary.week_start) fail('Publication plans must be ordered by week start');
      if (!manifest.selected_months.some(month => entry.summary.week_start <= `${month}-31` && entry.summary.week_end >= `${month}-01`)) fail('Published week does not overlap a selected month');
      weeks.add(entry.summary.week_start);
      const planUrl = safeRelative(manifestUrl,entry.plan_path,/^plans\/[A-Za-z0-9][A-Za-z0-9_-]*\/plan\.json$/);
      const shopUrl = safeRelative(manifestUrl,entry.shopping_path,/^plans\/[A-Za-z0-9][A-Za-z0-9_-]*\/shopping-list\.json$/);
      if (entry.plan_path !== `plans/${id}/plan.json` || entry.shopping_path !== `plans/${id}/shopping-list.json`) fail('Plan path identity mismatch');
      entries.set(id,{ summary:entry.summary, planUrl, shopUrl });
    }
    const candidate = { mode:'static',canEditPlans:false,publicationId:manifest.publication_id,exportedAt:manifest.exported_at,selectedMonths:manifest.selected_months,entries };
    for (const [id, entry] of entries) {
      const [plan, shopping] = await Promise.all([json(entry.planUrl, pinned ? 'publication_expired' : 'source_invalid'), json(entry.shopUrl, pinned ? 'publication_expired' : 'source_invalid')]);
      validatePair(plan, shopping);
      for (const key of ['plan_id','week_start','week_end','timezone','revision','snapshot_id','generated_at','seed']) if (plan[key] !== entry.summary[key]) fail(`Published plan ${id} does not match its summary`);
      entry.plan = plan; entry.shopping = shopping;
    }
    pinned = candidate; return pinned;
  }
  return {
    mode:'static',canEditPlans:false,
    get context() { return pinned && { mode:'static',publication_id:pinned.publicationId,exported_at:pinned.exportedAt,selected_months:pinned.selectedMonths }; },
    async refresh() { const old=pinned; try { return await load(true); } catch(e) { pinned=old; throw e; } },
    async listPlans(range) { validateRange(range); const source=await load(); const items=[...source.entries.values()].map(e=>e.summary).filter(s=>s.week_start<=range.to && s.week_end>=range.from).sort((a,b)=>a.week_start.localeCompare(b.week_start)); return {items}; },
    async getPlan(planId) { if (typeof planId !== 'string' || !idPattern.test(planId)) throw new ReaderError('invalid_request','Invalid plan identity.'); const source=await load(), entry=source.entries.get(planId); if (!entry) throw new ReaderError('plan_not_found','No published plan matches this identity.'); return structuredClone(entry.plan); },
    async getShoppingList(planId, revision, snapshotId) { if (typeof planId !== 'string' || !idPattern.test(planId) || !Number.isInteger(revision) || revision < 1 || typeof snapshotId !== 'string' || !snapshotId) throw new ReaderError('invalid_request','A valid plan identity, revision, and snapshot token are required.'); const source=await load(), entry=source.entries.get(planId); if (!entry) throw new ReaderError('plan_not_found','No published plan matches this identity.'); if (entry.summary.revision!==revision || entry.summary.snapshot_id!==snapshotId) throw new ReaderError('snapshot_changed','The pinned publication has a different snapshot. Refresh to load a coherent plan and grocery list.'); return structuredClone(entry.shopping); },
  };
}
