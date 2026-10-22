import test from 'node:test';
import assert from 'node:assert/strict';
import { authReducer, createAuthStore, initialAuthState, isUnauthenticated, messageForLoginError, parseRetryAfter, retryAfterMinutes } from '../src/auth/authModel.js';
import { createSessionApi } from '../src/auth/sessionApi.js';
import { createPlanEditor } from '../src/editing/planEditor.js';
import { authStore } from '../src/auth/authStore.js';

const json = (body, status = 200, headers = {}) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json', ...headers } });
const envelope = (code, message = code) => ({ error: { code, message, details: [] } });
function mockFetch(handler) {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.window = { location: { origin: 'http://meal.test' } };
  globalThis.fetch = async (input, init = {}) => { calls.push({ url: new URL(input, 'http://meal.test'), init }); return handler(calls.length, init); };
  return { calls, restore: () => { globalThis.fetch = original; delete globalThis.window; } };
}

test('Retry-After parses to whole minutes, rounded up and never below one', () => {
  assert.equal(parseRetryAfter('120'), 120);
  assert.equal(parseRetryAfter('abc'), null);
  assert.equal(parseRetryAfter(null), null);
  assert.equal(retryAfterMinutes(1), 1);
  assert.equal(retryAfterMinutes(60), 1);
  assert.equal(retryAfterMinutes(61), 2);
  assert.equal(retryAfterMinutes(900), 15);
  assert.equal(retryAfterMinutes(null), null);
});

test('login errors map to user-facing messages', () => {
  assert.equal(messageForLoginError({ code: 'invalid_credentials', status: 401 }), 'Wrong username or password.');
  assert.equal(messageForLoginError({ code: 'too_many_attempts', status: 429, retryAfter: 600 }), 'Too many attempts. Try again in 10 minutes.');
  assert.equal(messageForLoginError({ code: 'too_many_attempts', status: 429, retryAfter: 30 }), 'Too many attempts. Try again in 1 minute.');
  assert.equal(messageForLoginError({ code: 'too_many_attempts', status: 429, retryAfter: null }), 'Too many attempts. Try again in a few minutes.');
  assert.match(messageForLoginError({ code: 'cross_origin_rejected', status: 403 }), /blocked/);
  assert.match(messageForLoginError({ code: 'source_unavailable' }), /Could not reach the server/);
  assert.match(messageForLoginError({ status: 502 }), /server had a problem/);
  assert.equal(messageForLoginError({ status: 429 }), 'Too many attempts. Try again in a few minutes.');
  assert.equal(messageForLoginError(new Error('boom')), 'Could not reach the server. Check your connection and try again.');
  assert.equal(messageForLoginError({ code: 'weird', status: 400 }), 'Could not sign in. Try again.');
});

test('auth state: capabilities set the user, login and logout bump the reload token', () => {
  let state = authReducer(initialAuthState, { type: 'capabilities', authenticated: true, username: 'ana' });
  assert.equal(state.username, 'ana');
  assert.equal(authReducer(state, { type: 'capabilities', authenticated: true, username: 'ana' }), state);
  state = authReducer(state, { type: 'capabilities', authenticated: false });
  assert.equal(state.username, null);
  state = authReducer(state, { type: 'open_login' });
  assert.equal(state.loginOpen, true);
  state = authReducer(state, { type: 'logged_in', username: 'ana' });
  assert.deepEqual(state, { username: 'ana', loginOpen: false, reason: null, reloadToken: 1 });
  state = authReducer(state, { type: 'logged_out' });
  assert.deepEqual(state, { username: null, loginOpen: false, reason: null, reloadToken: 2 });
  assert.equal(authReducer(state, { type: 'nonsense' }), state);
});

test('expiry opens the login form once and cannot loop', () => {
  const signedIn = authReducer(initialAuthState, { type: 'capabilities', authenticated: true, username: 'ana' });
  const expired = authReducer(signedIn, { type: 'expired' });
  assert.deepEqual(expired, { username: null, loginOpen: true, reason: 'expired', reloadToken: 1 });
  assert.equal(authReducer(expired, { type: 'expired' }), expired);
  assert.equal(authReducer(expired, { type: 'capabilities', authenticated: false }), expired);
  const closed = authReducer(expired, { type: 'close_login' });
  assert.equal(closed.loginOpen, false); assert.equal(closed.reason, null);
});

test('the store notifies subscribers only on real changes', () => {
  const store = createAuthStore();
  let notifications = 0;
  const off = store.subscribe(() => { notifications += 1; });
  store.dispatch({ type: 'close_login' });
  store.dispatch({ type: 'open_login' });
  store.dispatch({ type: 'open_login' });
  off();
  store.dispatch({ type: 'close_login' });
  assert.equal(notifications, 1);
  assert.equal(isUnauthenticated(401, 'unauthenticated'), true);
  assert.equal(isUnauthenticated(401, 'invalid_credentials'), false);
  assert.equal(isUnauthenticated(403, 'unauthenticated'), false);
});

test('session client logs in, reads Retry-After, and sends same-origin JSON', async () => {
  const mock = mockFetch(n => n === 1 ? json({ username: 'ana' }) : n === 2 ? json(envelope('invalid_credentials'), 401) : n === 3 ? json(envelope('too_many_attempts'), 429, { 'Retry-After': '300' }) : new Response('bad gateway', { status: 502 }));
  try {
    const api = createSessionApi('/api/v1');
    assert.equal(await api.login('ana', 'pw'), 'ana');
    const first = mock.calls[0];
    assert.equal(first.url.pathname, '/api/v1/session'); assert.equal(first.init.method, 'POST');
    assert.equal(first.init.credentials, 'same-origin'); assert.equal(first.init.headers['Content-Type'], 'application/json');
    assert.deepEqual(JSON.parse(first.init.body), { username: 'ana', password: 'pw' });
    await assert.rejects(api.login('ana', 'x'), e => e.code === 'invalid_credentials' && e.status === 401);
    await assert.rejects(api.login('ana', 'x'), e => e.code === 'too_many_attempts' && e.retryAfter === 300 && messageForLoginError(e) === 'Too many attempts. Try again in 5 minutes.');
    await assert.rejects(api.login('ana', 'x'), e => e.status === 502 && /server had a problem/.test(messageForLoginError(e)));
  } finally { mock.restore(); }
});

test('session client: getSession returns null on 401, logout accepts 204 and 401, network errors are source_unavailable', async () => {
  const mock = mockFetch(n => n === 1 ? json(envelope('unauthenticated'), 401) : n === 2 ? json({ username: 'ana' }) : n === 3 ? new Response(null, { status: 204 }) : n === 4 ? json(envelope('unauthenticated'), 401) : Promise.reject(new TypeError('offline')));
  try {
    const api = createSessionApi();
    assert.equal(await api.getSession(), null);
    assert.equal(await api.getSession(), 'ana');
    await api.logout(); await api.logout();
    assert.equal(mock.calls[2].init.method, 'DELETE');
    await assert.rejects(api.getSession(), e => e.code === 'source_unavailable');
  } finally { mock.restore(); }
});

test('a 401 unauthenticated from the editing client puts the app back to login, other errors do not', async () => {
  authStore.dispatch({ type: 'capabilities', authenticated: true, username: 'ana' });
  const before = authStore.getState().reloadToken;
  const mock = mockFetch(n => n === 1 ? json(envelope('plan_not_found'), 404) : json(envelope('unauthenticated'), 401));
  try {
    const editor = createPlanEditor();
    await assert.rejects(editor.createPlan('2026-10-19'), e => e.code === 'plan_not_found');
    assert.equal(authStore.getState().username, 'ana');
    await assert.rejects(editor.createPlan('2026-10-19'), e => e.code === 'unauthenticated' && e.status === 401);
    assert.deepEqual(authStore.getState(), { username: null, loginOpen: true, reason: 'expired', reloadToken: before + 1 });
    await assert.rejects(editor.createPlan('2026-10-19'), e => e.code === 'unauthenticated');
    assert.equal(authStore.getState().reloadToken, before + 1);
    assert.equal(mock.calls.every(call => call.init.credentials !== 'omit'), true);
  } finally { mock.restore(); authStore.dispatch({ type: 'close_login' }); }
});
