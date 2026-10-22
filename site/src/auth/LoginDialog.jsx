import { useEffect, useRef, useState } from 'react';
import { Eye, EyeOff, Utensils, X } from 'lucide-react';
import { createSessionApi } from './sessionApi.js';
import { messageForLoginError } from './authModel.js';

const sessionApi = createSessionApi(import.meta.env.VITE_API_BASE_PATH || '/api/v1');
const FIELD = 'w-full min-h-11 px-3 rounded-lg border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-950 text-base text-slate-900 dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-indigo-500';

// Login form shown as a modal over the read-only app, so the current view is never lost.
export default function LoginDialog({ expired = false, onClose, onLoggedIn }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const usernameRef = useRef(null);

  useEffect(() => { usernameRef.current?.focus(); }, []);
  useEffect(() => {
    const onKey = event => { if (event.key === 'Escape' && !busy) onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [busy, onClose]);

  const submit = async event => {
    event.preventDefault();
    if (busy) return;
    setBusy(true); setError(null);
    try {
      const name = await sessionApi.login(username.trim(), password);
      setPassword('');
      onLoggedIn(name);
    } catch (err) {
      setError(messageForLoginError(err));
      setBusy(false);
      setPassword('');
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-slate-900/60 p-0 sm:p-4" onMouseDown={event => { if (event.target === event.currentTarget && !busy) onClose(); }}>
      <div role="dialog" aria-modal="true" aria-labelledby="login-title" className="w-full sm:max-w-sm max-h-full overflow-y-auto bg-white dark:bg-slate-900 text-slate-900 dark:text-slate-100 rounded-t-2xl sm:rounded-2xl shadow-xl border border-slate-200 dark:border-slate-800 p-6">
        <div className="flex items-start justify-between gap-3 mb-4">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-indigo-600 rounded-lg text-white"><Utensils size={20} /></div>
            <div>
              <h2 id="login-title" className="text-lg font-bold leading-none">Log in</h2>
              <p className="text-xs text-slate-500 dark:text-slate-400 mt-1">Meal Prep</p>
            </div>
          </div>
          <button type="button" onClick={onClose} disabled={busy} aria-label="Close" className="min-h-11 min-w-11 -mt-1 -mr-2 inline-flex items-center justify-center rounded-lg text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800 disabled:opacity-50"><X size={20} /></button>
        </div>
        {expired && <p role="status" className="mb-4 rounded-lg bg-amber-50 dark:bg-amber-950/30 text-amber-900 dark:text-amber-100 px-3 py-2 text-sm">Your session expired. Log in to continue.</p>}
        <form onSubmit={submit} method="post" className="space-y-4">
          <div>
            <label htmlFor="login-username" className="block text-sm font-medium mb-1">Username</label>
            <input ref={usernameRef} id="login-username" name="username" type="text" autoComplete="username" autoCapitalize="none" autoCorrect="off" spellCheck={false} required
              value={username} onChange={event => setUsername(event.target.value)} className={FIELD} />
          </div>
          <div>
            <label htmlFor="login-password" className="block text-sm font-medium mb-1">Password</label>
            <div className="relative">
              <input id="login-password" name="password" type={showPassword ? 'text' : 'password'} autoComplete="current-password" required
                value={password} onChange={event => setPassword(event.target.value)} className={`${FIELD} pr-12`} />
              <button type="button" onClick={() => setShowPassword(value => !value)} aria-pressed={showPassword} aria-label={showPassword ? 'Hide password' : 'Show password'}
                className="absolute right-0 top-0 min-h-11 min-w-11 inline-flex items-center justify-center text-slate-500 dark:text-slate-400">
                {showPassword ? <EyeOff size={20} /> : <Eye size={20} />}
              </button>
            </div>
          </div>
          <div role="alert" aria-live="assertive">{error && <p className="rounded-lg bg-red-50 dark:bg-red-950/30 text-red-800 dark:text-red-200 px-3 py-2 text-sm">{error}</p>}</div>
          <button type="submit" disabled={busy || !username || !password} className="w-full min-h-11 rounded-lg bg-indigo-600 text-white font-medium disabled:opacity-60">{busy ? 'Logging in…' : 'Log in'}</button>
        </form>
      </div>
    </div>
  );
}
