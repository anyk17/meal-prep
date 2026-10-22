// Session client for the home (API-mode) deployment. Cookies are HttpOnly, so JS only sees the username.
import { ReaderError } from '../readers/shared.js';
import { parseRetryAfter } from './authModel.js';

export function createSessionApi(base = '/api/v1') {
  const path = `${base.replace(/\/$/, '')}/session`;

  async function send(method, body) {
    const headers = { Accept: 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    let response;
    try { response = await fetch(path, { method, headers, cache: 'no-store', credentials: 'same-origin', body: body === undefined ? undefined : JSON.stringify(body) }); }
    catch { throw new ReaderError('source_unavailable', 'The meal plan API could not be reached.'); }
    return response;
  }
  async function failure(response) {
    let payload = null;
    try { payload = await response.json(); } catch { /* non-JSON body: fall back to the status */ }
    const code = typeof payload?.error?.code === 'string' ? payload.error.code : undefined;
    const error = new ReaderError(code, typeof payload?.error?.message === 'string' ? payload.error.message : `Request failed (${response.status}).`);
    error.status = response.status;
    error.retryAfter = parseRetryAfter(response.headers?.get?.('Retry-After'));
    return error;
  }
  async function readUser(response) {
    let payload;
    try { payload = await response.json(); } catch { throw new ReaderError('source_invalid', 'The API returned invalid JSON.'); }
    if (typeof payload?.username !== 'string' || !payload.username) throw new ReaderError('source_invalid', 'The API returned an invalid session.');
    return payload.username;
  }

  return {
    // Resolves to the username, or null when there is no valid session (401).
    async getSession() {
      const response = await send('GET');
      if (response.status === 401) return null;
      if (!response.ok) throw await failure(response);
      return readUser(response);
    },
    async login(username, password) {
      const response = await send('POST', { username, password });
      if (!response.ok) throw await failure(response);
      return readUser(response);
    },
    async logout() {
      const response = await send('DELETE');
      // 401 means the session is already gone, which is what logging out wanted.
      if (response.status !== 204 && response.status !== 401 && !response.ok) throw await failure(response);
    },
  };
}
