import { ReaderError, normalizeError, validatePlan, validateRange, validateShopping, validateSummary } from './shared.js';

async function request(url) {
  let response;
  try { response = await fetch(url, { cache: 'no-store' }); }
  catch { throw new ReaderError('source_unavailable', 'The meal plan API could not be reached.'); }
  let body; try { body = await response.json(); } catch {
    // A non-JSON error page comes from a proxy in front of an unreachable app server, not from the API itself.
    if (!response.ok) throw new ReaderError('source_unavailable', `The meal plan API could not be reached (${response.status}).`);
    throw new ReaderError('source_invalid', 'The API returned invalid JSON.');
  }
  if (!response.ok) {
    const error = body?.error;
    const validCodes = ['invalid_request','plan_not_found','snapshot_changed','source_unavailable','source_invalid'];
    if (!error || typeof error.message !== 'string' || !Array.isArray(error.details) || !validCodes.includes(error.code) || error.details.some(detail => !detail || typeof detail.field !== 'string' || typeof detail.message !== 'string')) throw new ReaderError('source_invalid','The API returned an invalid error response.');
    const code = response.status === 409 ? 'snapshot_changed' : error.code;
    throw normalizeError(code, error.message || `API request failed (${response.status}).`, error.details);
  }
  return body;
}
export function createApiReader(base = '/api/v1') {
  const path = base.replace(/\/$/, '');
  const origin = typeof window !== 'undefined' ? window.location.origin : 'http://localhost';
  const url = value => new URL(value,origin).href;
  return {
    mode: 'api', canEditPlans: false,
    async listPlans(range) { validateRange(range); const result = await request(url(`${path}/plans?from=${encodeURIComponent(range.from)}&to=${encodeURIComponent(range.to)}`)); if (!result || Object.keys(result).join(',') !== 'items' || !Array.isArray(result.items)) throw new ReaderError('source_invalid','Invalid plan list response'); result.items.forEach(validateSummary); if (result.items.some(item=>item.week_start>range.to||item.week_end<range.from)) throw new ReaderError('source_invalid','API returned a plan outside the requested range.'); result.items.sort((a,b)=>a.week_start.localeCompare(b.week_start)); return result; },
    async getPlan(planId) { if (typeof planId !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(planId)) throw new ReaderError('invalid_request','Invalid plan identity.'); const p = await request(url(`${path}/plans/${encodeURIComponent(planId)}`)); validatePlan(p); if (p.plan_id !== planId) throw new ReaderError('source_invalid','API returned a different plan identity'); return p; },
    async getShoppingList(planId, revision, snapshotId) {
      if (typeof planId !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(planId) || !Number.isInteger(revision) || revision < 1 || typeof snapshotId !== 'string' || !snapshotId) throw new ReaderError('invalid_request','A valid plan identity, revision, and snapshot token are required.');
      const shop = await request(url(`${path}/plans/${encodeURIComponent(planId)}/shopping-list?revision=${revision}&snapshot_id=${encodeURIComponent(snapshotId)}`));
      validateShopping(shop);
      if (shop.plan_id !== planId || shop.revision !== revision || shop.snapshot_id !== snapshotId) throw new ReaderError('source_invalid','API returned groceries for a different snapshot.');
      return shop;
    },
  };
}
