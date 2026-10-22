// Write client for the home (API-mode) deployment only. It is deliberately separate from the
// read-only reader interface and is only ever imported through a dynamic import that the static
// (GitHub Pages) build removes.
import { createTripBody, addWeekBody, removeWeekPath, purchaseBody } from '../tripModel.js';
import { ReaderError, normalizeError, validatePair, validateTrip } from '../readers/shared.js';
import { notifyIfUnauthenticated } from '../auth/authStore.js';

const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const MONTH_PATTERN = /^\d{4}-(0[1-9]|1[0-2])$/;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

function requireId(value, label = 'identity') {
  if (typeof value !== 'string' || !ID_PATTERN.test(value)) throw new ReaderError('invalid_request', `Invalid ${label}.`);
  return value;
}
function requireRevision(value) {
  if (!Number.isInteger(value) || value < 1) throw new ReaderError('invalid_request', 'A valid expected revision is required.');
  return value;
}
function requireVersion(value) {
  if (!Number.isInteger(value) || value < 1) throw new ReaderError('invalid_request', 'A valid expected shopping list version is required.');
  return value;
}
function checkTrip(trip, tripId) {
  validateTrip(trip);
  if (tripId !== undefined && trip.trip_id !== tripId) throw new ReaderError('source_invalid', 'The API returned a different shopping list.');
  return trip;
}
function newKey() {
  const crypto = globalThis.crypto;
  if (!crypto?.randomUUID) throw new ReaderError('invalid_request', 'This browser cannot generate an idempotency key.');
  return crypto.randomUUID();
}
function parseRetryAfter(value) {
  const seconds = Number.parseInt(value ?? '', 10);
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : null;
}

export function createPlanEditor(base = '/api/v1') {
  const path = base.replace(/\/$/, '');
  const origin = typeof window !== 'undefined' && window.location?.origin ? window.location.origin : 'http://localhost';
  const url = value => new URL(value, origin).href;
  // One Idempotency-Key per user action. The key is kept while the outcome is unknown (network
  // failure, proxy error, 503) so a retry of the same action replays instead of repeating it.
  const pendingKeys = new Map();

  async function purchase(tripId, expectedVersion, cells, action) {
    requireId(tripId, 'shopping list identity'); requireVersion(expectedVersion);
    let body;
    try { body = purchaseBody(expectedVersion, cells); } catch (error) { throw new ReaderError('invalid_request', error.message); }
    body.cells.forEach(cell => requireId(cell.plan_id, 'plan identity'));
    return checkTrip(await request('POST', `/shopping-trips/${encodeURIComponent(tripId)}/${action}`, { body }), tripId);
  }

  async function request(method, endpoint, { body, idempotencyAction } = {}) {
    const headers = { Accept: 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    let key;
    if (idempotencyAction) {
      key = pendingKeys.get(idempotencyAction) ?? newKey();
      pendingKeys.set(idempotencyAction, key);
      headers['Idempotency-Key'] = key;
    }
    const settle = () => { if (idempotencyAction) pendingKeys.delete(idempotencyAction); };
    let response;
    try { response = await fetch(url(`${path}${endpoint}`), { method, headers, cache: 'no-store', body: body === undefined ? undefined : JSON.stringify(body) }); }
    catch { throw new ReaderError('source_unavailable', 'The meal plan API could not be reached.'); }
    if (response.status === 204) { settle(); return null; }
    let payload;
    try { payload = await response.json(); }
    catch {
      if (!response.ok) throw new ReaderError('source_unavailable', `The meal plan API could not be reached (${response.status}).`);
      settle();
      throw new ReaderError('source_invalid', 'The API returned invalid JSON.');
    }
    if (!response.ok) {
      const error = payload?.error;
      if (!error || typeof error.code !== 'string' || typeof error.message !== 'string' || !Array.isArray(error.details)) {
        throw new ReaderError('source_invalid', 'The API returned an invalid error response.');
      }
      // 503 means nothing was committed and a retry of the same action is expected.
      if (response.status !== 503) settle();
      notifyIfUnauthenticated(response.status, error.code);
      const normalized = normalizeError(error.code, error.message, error.details);
      normalized.status = response.status;
      normalized.retryAfter = parseRetryAfter(response.headers?.get?.('Retry-After'));
      throw normalized;
    }
    settle();
    return payload;
  }

  function checkMutation(result, planId) {
    if (!result || typeof result !== 'object' || result.plan_id !== planId || !Number.isInteger(result.revision) || typeof result.snapshot_id !== 'string' || typeof result.changed !== 'boolean') {
      throw new ReaderError('source_invalid', 'The API returned an invalid mutation result.');
    }
    return result;
  }

  return {
    async getCapabilities() {
      const result = await request('GET', '/capabilities');
      return { canEditPlans: result?.can_edit_plans === true, canEditShoppingTrips: result?.can_edit_shopping_trips === true, canPublish: result?.can_publish === true, authenticated: result?.authenticated === true, username: typeof result?.username === 'string' ? result.username : null };
    },
    async createPlan(weekStart, seed) {
      if (typeof weekStart !== 'string' || !DATE_PATTERN.test(weekStart)) throw new ReaderError('invalid_request', 'A week start date is required.');
      const body = { week_start: weekStart };
      if (seed !== undefined) body.seed = seed;
      return request('POST', '/plans', { body });
    },
    async propose(planId, expectedRevision, seed) {
      requireId(planId); requireRevision(expectedRevision);
      const body = { expected_revision: expectedRevision };
      if (seed !== undefined) body.seed = seed;
      const proposal = await request('POST', `/plans/${encodeURIComponent(planId)}/proposals`, { body });
      return checkProposal(proposal, planId);
    },
    async getProposal(planId, proposalId) {
      requireId(planId); requireId(proposalId, 'proposal identity');
      return checkProposal(await request('GET', `/plans/${encodeURIComponent(planId)}/proposals/${encodeURIComponent(proposalId)}`), planId);
    },
    async discardProposal(planId, proposalId) {
      requireId(planId); requireId(proposalId, 'proposal identity');
      return request('DELETE', `/plans/${encodeURIComponent(planId)}/proposals/${encodeURIComponent(proposalId)}`);
    },
    async acceptProposal(planId, proposalId, expectedRevision) {
      requireId(planId); requireId(proposalId, 'proposal identity'); requireRevision(expectedRevision);
      const result = await request('POST', `/plans/${encodeURIComponent(planId)}/proposals/${encodeURIComponent(proposalId)}/accept`, {
        body: { expected_revision: expectedRevision }, idempotencyAction: `accept:${planId}:${proposalId}:${expectedRevision}`,
      });
      return checkMutation(result, planId);
    },
    async undo(planId, expectedRevision) {
      requireId(planId); requireRevision(expectedRevision);
      const result = await request('POST', `/plans/${encodeURIComponent(planId)}/undo`, {
        body: { expected_revision: expectedRevision }, idempotencyAction: `undo:${planId}:${expectedRevision}`,
      });
      return checkMutation(result, planId);
    },
    async listRevisions(planId) {
      requireId(planId);
      const result = await request('GET', `/plans/${encodeURIComponent(planId)}/revisions`);
      if (!result || result.plan_id !== planId || !Array.isArray(result.items)) throw new ReaderError('source_invalid', 'The API returned an invalid revision list.');
      return result;
    },
    // ---- Shopping trips. Every write carries the trip version the user is looking at; 409 stale_version means refetch. ----
    async createTrip(planIds, name, groups) {
      if (!Array.isArray(planIds) || !planIds.length || planIds.length > 60 || new Set(planIds).size !== planIds.length) throw new ReaderError('invalid_request', 'Choose at least one week.');
      planIds.forEach(id => requireId(id, 'plan identity'));
      return checkTrip(await request('POST', '/shopping-trips', { body: createTripBody(planIds, name, groups) }));
    },
    async addTripWeek(tripId, planId, expectedVersion, groups) {
      requireId(tripId, 'shopping list identity'); requireId(planId, 'plan identity'); requireVersion(expectedVersion);
      return checkTrip(await request('POST', `/shopping-trips/${encodeURIComponent(tripId)}/weeks`, { body: addWeekBody(planId, expectedVersion, groups) }), tripId);
    },
    // `group` removes one shelf-life group from the week; without it the whole week leaves the list.
    async removeTripWeek(tripId, planId, expectedVersion, group) {
      requireId(tripId, 'shopping list identity'); requireId(planId, 'plan identity'); requireVersion(expectedVersion);
      return checkTrip(await request('DELETE', removeWeekPath(tripId, planId, expectedVersion, group)), tripId);
    },
    async markTripDone(tripId, expectedVersion) {
      requireId(tripId, 'shopping list identity'); requireVersion(expectedVersion);
      return checkTrip(await request('POST', `/shopping-trips/${encodeURIComponent(tripId)}/done`, { body: { expected_version: expectedVersion } }), tripId);
    },
    async reopenTrip(tripId, expectedVersion) {
      requireId(tripId, 'shopping list identity'); requireVersion(expectedVersion);
      return checkTrip(await request('POST', `/shopping-trips/${encodeURIComponent(tripId)}/reopen`, { body: { expected_version: expectedVersion } }), tripId);
    },
    // Mark cells [{plan_id, group}] bought, or return them to to-buy (undo). Both carry the trip version.
    async markCellsBought(tripId, expectedVersion, cells) { return purchase(tripId, expectedVersion, cells, 'purchases'); },
    async undoCellsBought(tripId, expectedVersion, cells) { return purchase(tripId, expectedVersion, cells, 'purchases/undo'); },
    async deleteTrip(tripId, expectedVersion) {
      requireId(tripId, 'shopping list identity'); requireVersion(expectedVersion);
      await request('DELETE', `/shopping-trips/${encodeURIComponent(tripId)}?expected_version=${expectedVersion}`);
    },
    // ---- Meal swaps. Preview is stateless; apply carries expected_revision and an Idempotency-Key. ----
    async getCatalogVariants() {
      const result = await request('GET', '/catalog/variants');
      if (!result || !Array.isArray(result.items)) throw new ReaderError('source_invalid', 'The API returned an invalid recipe catalog.');
      return result.items;
    },
    async previewSwap(planId, swap) {
      const body = swapBody(planId, swap);
      const result = await request('POST', `/plans/${encodeURIComponent(planId)}/swaps/preview`, { body });
      if (!result || result.plan_id !== planId || !Array.isArray(result.changes) || !Array.isArray(result.grocery_delta) || !Array.isArray(result.rule_warnings)) {
        throw new ReaderError('source_invalid', 'The API returned an invalid swap preview.');
      }
      return result;
    },
    async applySwap(planId, swap) {
      const body = swapBody(planId, swap);
      const result = await request('POST', `/plans/${encodeURIComponent(planId)}/swaps`, {
        body, idempotencyAction: `swap:${planId}:${body.slot_id}:${body.variant_id}:${body.scope}:${body.expected_revision}`,
      });
      if (!result || typeof result !== 'object' || !result.plan || result.plan.plan_id !== planId || !Number.isInteger(result.plan.revision)) {
        throw new ReaderError('source_invalid', 'The API returned an invalid swap result.');
      }
      return { plan: result.plan, ruleWarnings: Array.isArray(result.rule_warnings) ? result.rule_warnings : [] };
    },
    async fillMonth(month) {
      if (typeof month !== 'string' || !MONTH_PATTERN.test(month)) throw new ReaderError('invalid_request', 'A month in YYYY-MM form is required.');
      const result = await request('POST', `/months/${month}/fill`, { body: {} });
      if (!result || result.month !== month || !Array.isArray(result.weeks)) throw new ReaderError('source_invalid', 'The API returned an invalid month result.');
      return result;
    },
  };
}

function swapBody(planId, { slotId, variantId, scope = 'pair', expectedRevision } = {}) {
  requireId(planId);
  requireId(slotId, 'slot identity');
  requireId(variantId, 'recipe variant identity');
  if (scope !== 'pair' && scope !== 'single') throw new ReaderError('invalid_request', 'Swap scope must be pair or single.');
  requireRevision(expectedRevision);
  return { slot_id: slotId, variant_id: variantId, scope, expected_revision: expectedRevision };
}

function checkProposal(proposal, planId) {
  if (!proposal || typeof proposal !== 'object' || proposal.plan_id !== planId || typeof proposal.proposal_id !== 'string' || !Number.isInteger(proposal.base_revision) || !Array.isArray(proposal.changes) || typeof proposal.identical !== 'boolean') {
    throw new ReaderError('source_invalid', 'The API returned an invalid proposal.');
  }
  validatePair(proposal.plan, proposal.shopping_list);
  proposal.replaced_swaps = Number.isInteger(proposal.replaced_swaps) && proposal.replaced_swaps > 0 ? proposal.replaced_swaps : 0;
  return proposal;
}

