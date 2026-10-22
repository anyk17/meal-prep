import { ReaderError, validTimestamp, validatePair, validateRange, validateSummary, validateTrip, validTripId, deriveCoverage } from './shared.js';

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
    const manifestKeys = 'exported_at,plans,publication_id,schema_version,selected_months,shopping_trips,timezone';
    if (!manifest || Object.keys(manifest).sort().join(',') !== manifestKeys || manifest.schema_version !== 1 || manifest.publication_id !== index.publication_id || !Array.isArray(manifest.plans) || !Array.isArray(manifest.shopping_trips) || !Array.isArray(manifest.selected_months) || !manifest.selected_months.length || new Set(manifest.selected_months).size !== manifest.selected_months.length || manifest.selected_months.some((month,i) => typeof month !== 'string' || !/^\d{4}-(0[1-9]|1[0-2])$/.test(month) || month.startsWith('0000') || (i > 0 && manifest.selected_months[i-1] >= month)) || typeof manifest.timezone !== 'string' || !manifest.timezone || !validTimezone(manifest.timezone) || !validTimestamp(manifest.exported_at)) fail('Invalid publication manifest');
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
    const tripEntries = new Map(), tripWeeks = new Set();
    for (const entry of manifest.shopping_trips) {
      if (!entry || Object.keys(entry).sort().join(',') !== 'path,trip_id' || !validTripId(entry.trip_id) || tripEntries.has(entry.trip_id) || entry.path !== `shopping-trips/${entry.trip_id}.json`) fail('Invalid publication shopping trip entry');
      tripEntries.set(entry.trip_id, { url: safeRelative(manifestUrl, entry.path, /^shopping-trips\/[A-Za-z0-9][A-Za-z0-9_-]*\.json$/) });
    }
    const candidate = { mode:'static',canEditPlans:false,publicationId:manifest.publication_id,exportedAt:manifest.exported_at,selectedMonths:manifest.selected_months,entries, trips:tripEntries };
    for (const [id, entry] of entries) {
      const [plan, shopping] = await Promise.all([json(entry.planUrl, pinned ? 'publication_expired' : 'source_invalid'), json(entry.shopUrl, pinned ? 'publication_expired' : 'source_invalid')]);
      validatePair(plan, shopping);
      for (const key of ['plan_id','week_start','week_end','timezone','revision','snapshot_id','generated_at','seed']) if (plan[key] !== entry.summary[key]) fail(`Published plan ${id} does not match its summary`);
      entry.plan = plan; entry.shopping = shopping;
    }
    for (const [tripId, tripEntry] of tripEntries) {
      const trip = await json(tripEntry.url, pinned ? 'publication_expired' : 'source_invalid');
      validateTrip(trip);
      if (trip.trip_id !== tripId) fail(`Published shopping trip ${tripId} does not match its identity`);
      for (const week of trip.weeks) {
        const planEntry = entries.get(week.plan_id);
        if (!planEntry) fail(`Shopping trip ${tripId} covers a week that is not published`);
        if (planEntry.summary.week_start !== week.week_start || planEntry.summary.week_end !== week.week_end) fail(`Shopping trip ${tripId} disagrees with the published plan dates`);
        if (week.cells.some(cell => cell.state === 'to_buy' && (week.current_revision !== planEntry.summary.revision || (cell.snapshot_id !== null && cell.snapshot_id !== planEntry.summary.snapshot_id)))) fail(`Shopping trip ${tripId} has a to-buy week that is not coherent with the published plan`);
        for (const group of week.groups) {
          if (tripWeeks.has(`${week.plan_id}|${group}`)) fail('A published week is on more than one shopping trip');
          tripWeeks.add(`${week.plan_id}|${group}`);
        }
      }
      tripEntry.trip = trip;
    }
    pinned = candidate; return pinned;
  }
  return {
    mode:'static',canEditPlans:false,
    get context() { return pinned && { mode:'static',publication_id:pinned.publicationId,exported_at:pinned.exportedAt,selected_months:pinned.selectedMonths }; },
    async refresh() { const old=pinned; try { return await load(true); } catch(e) { pinned=old; throw e; } },
    async listPlans(range) { validateRange(range); const source=await load(); const items=[...source.entries.values()].map(e=>e.summary).filter(s=>s.week_start<=range.to && s.week_end>=range.from).sort((a,b)=>a.week_start.localeCompare(b.week_start)); return {items}; },
    async getPlan(planId) { if (typeof planId !== 'string' || !idPattern.test(planId)) throw new ReaderError('invalid_request','Invalid plan identity.'); const source=await load(), entry=source.entries.get(planId); if (!entry) throw new ReaderError('plan_not_found','No published plan matches this identity.'); return structuredClone(entry.plan); },
    async listShoppingTrips() { const source=await load(); return {items:[...source.trips.values()].map(e=>structuredClone(e.trip))}; },
    async getShoppingTrip(tripId) { if (!validTripId(tripId)) throw new ReaderError('invalid_request','Invalid shopping trip identity.'); const source=await load(), entry=source.trips.get(tripId); if (!entry) throw new ReaderError('trip_not_found','No published shopping list matches this identity.'); return structuredClone(entry.trip); },
    async getCoverage(range) { validateRange(range); const source=await load(); return deriveCoverage([...source.entries.values()].map(e=>e.summary),[...source.trips.values()].map(e=>e.trip),range); },
    async getShoppingList(planId, revision, snapshotId) { if (typeof planId !== 'string' || !idPattern.test(planId) || !Number.isInteger(revision) || revision < 1 || typeof snapshotId !== 'string' || !snapshotId) throw new ReaderError('invalid_request','A valid plan identity, revision, and snapshot token are required.'); const source=await load(), entry=source.entries.get(planId); if (!entry) throw new ReaderError('plan_not_found','No published plan matches this identity.'); if (entry.summary.revision!==revision || entry.summary.snapshot_id!==snapshotId) throw new ReaderError('snapshot_changed','The pinned publication has a different snapshot. Refresh to load a coherent plan and grocery list.'); return structuredClone(entry.shopping); },
  };
}
