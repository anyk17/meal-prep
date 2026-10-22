// Pure household-login logic: error -> message mapping, Retry-After parsing, and the auth state machine.
// No React, no fetch, no import.meta, so it is unit-tested with node:test and safe to share with the static build.

export function parseRetryAfter(value) {
  const seconds = Number.parseInt(value ?? '', 10);
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : null;
}

// Whole minutes to show for a Retry-After in seconds; never 0 so the user is not told to retry "in 0 minutes".
export function retryAfterMinutes(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return null;
  return Math.max(1, Math.ceil(seconds / 60));
}

export function messageForLoginError(error) {
  const code = error?.code;
  const status = error?.status;
  if (code === 'invalid_credentials' || (!code && status === 401)) return 'Wrong username or password.';
  if (code === 'too_many_attempts' || (!code && status === 429)) {
    const minutes = retryAfterMinutes(error?.retryAfter);
    if (minutes === null) return 'Too many attempts. Try again in a few minutes.';
    return `Too many attempts. Try again in ${minutes} ${minutes === 1 ? 'minute' : 'minutes'}.`;
  }
  if (code === 'cross_origin_rejected' || (!code && status === 403)) return 'The sign-in was blocked. Reload the page and try again.';
  if (code === 'source_unavailable' || (status === undefined && code === undefined)) return 'Could not reach the server. Check your connection and try again.';
  if (typeof status === 'number' && status >= 500) return 'The server had a problem. Try again in a moment.';
  return 'Could not sign in. Try again.';
}

export const isUnauthenticated = (status, code) => status === 401 && code === 'unauthenticated';

// Login is only needed to change things; viewing is open. State: who is signed in (from /capabilities or a login),
// whether the login form is open, and why. reloadToken bumps whenever the app should re-read capabilities and data.
export const initialAuthState = Object.freeze({ username: null, loginOpen: false, reason: null, reloadToken: 0 });

export function authReducer(state, action) {
  switch (action.type) {
    case 'capabilities': {
      const username = action.authenticated && action.username ? action.username : null;
      return username === state.username ? state : { ...state, username };
    }
    case 'open_login':
      return state.loginOpen ? state : { ...state, loginOpen: true, reason: null };
    case 'close_login':
      return state.loginOpen ? { ...state, loginOpen: false, reason: null } : state;
    case 'logged_in':
      return { username: action.username, loginOpen: false, reason: null, reloadToken: state.reloadToken + 1 };
    // A 401 on a write: back to read-only and ask to sign in again. Ignored while the form is already open,
    // so a burst of failing calls cannot loop.
    case 'expired':
      return state.loginOpen ? state : { username: null, loginOpen: true, reason: 'expired', reloadToken: state.reloadToken + 1 };
    case 'logged_out':
      return { username: null, loginOpen: false, reason: null, reloadToken: state.reloadToken + 1 };
    default:
      return state;
  }
}

export function createAuthStore(initial = initialAuthState) {
  let state = initial;
  const listeners = new Set();
  return {
    getState: () => state,
    dispatch(action) {
      const next = authReducer(state, action);
      if (next === state) return;
      state = next;
      listeners.forEach(listener => listener());
    },
    subscribe(listener) { listeners.add(listener); return () => { listeners.delete(listener); }; },
  };
}
