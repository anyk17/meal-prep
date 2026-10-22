import { useState, useEffect, useRef, useCallback, useSyncExternalStore, lazy, Suspense } from 'react';
import { MealCard } from './components/MealCard';
import { ShoppingList } from './components/ShoppingList';
import { RecipeModal } from './components/RecipeModal';
import { WeekGrid } from './components/WeekGrid';
import { MonthView } from './components/MonthView';
import { TripsView } from './components/TripsView';
import { coverageSummary, weekSelectable } from './tripModel.js';
import { CoverageBadge, WeekCoverage } from './components/CoverageBadge';
import { Utensils, Calendar, Moon, Sun, ChevronLeft, ChevronRight, LogIn, LogOut } from 'lucide-react';
import { createApiReader } from './readers/api';
import { createStaticReader } from './readers/static';
import { validatePair, addDays } from './readers/shared.js';
import { authStore } from './auth/authStore.js';
import { mondayOf, todayIn, formatWeekRange, formatMonth, monthOf } from './dates.js';

const createReader = () => import.meta.env.VITE_READER_MODE === 'static'
  ? createStaticReader('publication/index.json')
  : createApiReader(import.meta.env.VITE_API_BASE_PATH || '/api/v1');
let reader = createReader();
const INITIAL_WEEK = mondayOf(import.meta.env.VITE_DEFAULT_FROM || '2026-10-19');

// Editing is home-only. In the static (Pages) build these constants fold to null at build time, so
// the write client and editing UI are never bundled and cannot be enabled at runtime.
const loadEditor = import.meta.env.VITE_READER_MODE === 'static' ? null : () => import('./editing/planEditor.js');
const EditingPanel = import.meta.env.VITE_READER_MODE === 'static' ? null : lazy(() => import('./editing/EditingPanel.jsx'));
const TripControls = import.meta.env.VITE_READER_MODE === 'static' ? null : lazy(() => import('./editing/TripControls.jsx'));
const SwapDialog = import.meta.env.VITE_READER_MODE === 'static' ? null : lazy(() => import('./editing/SwapDialog.jsx'));
const CreateTripBar = import.meta.env.VITE_READER_MODE === 'static' ? null : lazy(() => import('./editing/TripControls.jsx').then(module => ({ default: module.CreateTripBar })));
const LoginDialog = import.meta.env.VITE_READER_MODE === 'static' ? null : lazy(() => import('./auth/LoginDialog.jsx'));
const loadSessionApi = import.meta.env.VITE_READER_MODE === 'static' ? null : () => import('./auth/sessionApi.js');
const PublishView = import.meta.env.VITE_READER_MODE === 'static' ? null : lazy(() => import('./editing/PublishView.jsx'));
const MonthFill = import.meta.env.VITE_READER_MODE === 'static' ? null : lazy(() => import('./editing/EditingPanel.jsx').then(module => ({ default: module.MonthFill })));

function formatLocalDateTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short'
  }).format(date);
}
function getGarbageCollectorLabel(date = new Date()) { return date.getDate() % 2 === 0 ? 'J' : 'T'; }
function readTheme() {
  try { const saved = window.localStorage.getItem('theme'); if (saved === 'light' || saved === 'dark') return saved; } catch { /* browser storage may be disabled */ }
  try { if (window.matchMedia('(prefers-color-scheme: dark)').matches) return 'dark'; } catch { /* optional browser feature */ }
  return 'light';
}

function App() {
  const [plan, setPlan] = useState(null);
  const [shoppingList, setShoppingList] = useState(null);
  const [selectedSlot, setSelectedSlot] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [sourceContext, setSourceContext] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const [weekStart, setWeekStart] = useState(INITIAL_WEEK);
  const [loaded, setLoaded] = useState(false);
  const [canEdit, setCanEdit] = useState(false);
  const [canEditTrips, setCanEditTrips] = useState(false);
  const [canPublish, setCanPublish] = useState(false);
  const [tripId, setTripId] = useState(null);
  const [tripNonce, setTripNonce] = useState(0);
  const [selectedPlans, setSelectedPlans] = useState([]);
  const [newListHint, setNewListHint] = useState(false);
  const [monthCoverage, setMonthCoverage] = useState(() => new Map());
  const [review, setReview] = useState(null);
  const [swapSlot, setSwapSlot] = useState(null);
  const [swapNotice, setSwapNotice] = useState(null);
  const [view, setView] = useState('week');
  const [month, setMonth] = useState(monthOf(INITIAL_WEEK));
  const [monthNonce, setMonthNonce] = useState(0);
  const auth = useSyncExternalStore(authStore.subscribe, authStore.getState);
  const [loggingOut, setLoggingOut] = useState(false);
  const [logoutError, setLogoutError] = useState(null);
  const requestGeneration = useRef(0);
  const hasPlan = useRef(false);
  const garbageCollector = getGarbageCollectorLabel();

  // Theme state
  const [theme, setTheme] = useState(readTheme);

  useEffect(() => {
    const root = window.document.documentElement;
    if (theme === 'dark') {
      root.classList.add('dark');
    } else {
      root.classList.remove('dark');
    }
    try { window.localStorage.setItem('theme', theme); } catch { /* browser storage may be disabled */ }
  }, [theme]);

  const toggleTheme = () => {
    setTheme(prev => prev === 'light' ? 'dark' : 'light');
  };

  const loadData = useCallback(async (refresh = false) => {
    const generation = ++requestGeneration.current;
    setRefreshing(refresh);
    if (!hasPlan.current) setLoading(true);
    try {
      const candidateReader = refresh && reader.mode === 'static' ? createReader() : reader;
      if (refresh && candidateReader.mode === 'static') await candidateReader.refresh();
      const range = { from: weekStart, to: addDays(weekStart, 6) };
      const result = await candidateReader.listPlans(range);
      let selected = result.items.find(item => item.week_start === weekStart);
      if (generation !== requestGeneration.current) return;
      if (!selected) { reader = candidateReader; hasPlan.current=false; setPlan(null); setShoppingList(null); setSourceContext(candidateReader.context); setSelectedSlot(null); setError(null); setLoaded(true); return; }
      let nextPlan, nextShop;
      for (let attempt = 0; attempt < (candidateReader.mode === 'api' ? 2 : 1); attempt += 1) {
        nextPlan = await candidateReader.getPlan(selected.plan_id);
        try { nextShop = await candidateReader.getShoppingList(selected.plan_id, nextPlan.revision, nextPlan.snapshot_id); break; }
        catch (err) {
          if (err.code !== 'snapshot_changed' || attempt > 0 || candidateReader.mode !== 'api') throw err;
          const refreshed = await candidateReader.listPlans(range);
          selected = refreshed.items.find(item => item.plan_id === selected.plan_id) || refreshed.items.find(item => item.week_start === weekStart);
          if (!selected) throw err;
        }
      }
      if (generation !== requestGeneration.current) return;
      validatePair(nextPlan,nextShop);
      reader = candidateReader;
      hasPlan.current=true;
      setPlan(nextPlan);
      setShoppingList(nextShop);
      setSourceContext(candidateReader.context);
      setSelectedSlot(null);
      setError(null);
      setLoaded(true);
    } catch (err) {
      if (generation !== requestGeneration.current) return;
      setError(err?.message || 'Could not load the meal plan.');
    } finally { if (generation === requestGeneration.current) { setLoading(false); setRefreshing(false); } }
  }, [weekStart]);

  useEffect(() => { void loadData(); return () => { requestGeneration.current += 1; }; }, [loadData]);

  // Ask the server whether this client may edit. Hiding controls is not authorization; the server enforces it.
  const refreshCapabilities = useCallback(async () => {
    if (!loadEditor) return;
    try { const { createPlanEditor } = await loadEditor(); const caps = await createPlanEditor(import.meta.env.VITE_API_BASE_PATH || '/api/v1').getCapabilities(); setCanEdit(caps.canEditPlans); setCanEditTrips(caps.canEditShoppingTrips); setCanPublish(caps.canPublish); authStore.dispatch({ type: 'capabilities', authenticated: caps.authenticated, username: caps.username }); }
    catch { setCanEdit(false); setCanEditTrips(false); setCanPublish(false); }
  }, []);
  useEffect(() => { void refreshCapabilities(); }, [refreshCapabilities]);

  // Login, logout, or an expired session changes what the user may do: drop editing state and re-read capabilities and data.
  const reloadToken = auth.reloadToken;
  const seenReloadToken = useRef(reloadToken);
  useEffect(() => {
    if (seenReloadToken.current === reloadToken) return;
    seenReloadToken.current = reloadToken;
    void refreshCapabilities();
    void loadData(true);
    setReview(null); setSwapSlot(null); setSwapNotice(null); setSelectedPlans([]); setMonthNonce(value => value + 1);
  }, [reloadToken, refreshCapabilities, loadData]);
  const openLogin = () => authStore.dispatch({ type: 'open_login' });
  const closeLogin = useCallback(() => authStore.dispatch({ type: 'close_login' }), []);
  const logout = async () => {
    if (loggingOut || !loadSessionApi) return;
    setLoggingOut(true); setLogoutError(null);
    try { const { createSessionApi } = await loadSessionApi(); await createSessionApi(import.meta.env.VITE_API_BASE_PATH || '/api/v1').logout(); authStore.dispatch({ type: 'logged_out' }); }
    catch { setLogoutError('Could not log out. Check your connection and try again.'); }
    finally { setLoggingOut(false); }
  };

  const goToWeek = target => {
    if (target === weekStart) return;
    hasPlan.current = false;
    setReview(null); setPlan(null); setShoppingList(null); setError(null); setSelectedSlot(null); setSwapSlot(null); setSwapNotice(null);
    setWeekStart(target);
  };
  const reload = async (refresh = false) => { if (refresh) void refreshCapabilities(); await loadData(refresh); };
  const showShopping = () => { setReview(null); setNewListHint(false); setView('shopping'); };
  const showPublish = () => { setReview(null); setNewListHint(false); setView('publish'); };
  const openTrip = id => { setReview(null); setNewListHint(false); setTripId(id); setView('shopping'); };
  const togglePlan = id => setSelectedPlans(previous => previous.includes(id) ? previous.filter(item => item !== id) : [...previous, id]);
  const showMonth = () => { setReview(null); setMonth(monthOf(weekStart)); setView('month'); };
  const openWeek = target => { setNewListHint(false); if (target === weekStart) void loadData(); else goToWeek(target); setView('week'); };
  // Month view reads through the shared reader; a static refresh re-pins the publication, then the month reloads.
  const reloadMonth = async (refresh = false) => {
    if (refresh) void refreshCapabilities();
    if (refresh && reader.mode === 'static') {
      try { const candidate = createReader(); await candidate.refresh(); reader = candidate; setError(null); }
      catch (err) { setError(err?.message || 'Could not refresh the publication.'); return; }
    }
    setMonthNonce(value => value + 1);
  };

  const renderWeekAccessory = row => (
    <>
      {canEditTrips && CreateTripBar && row.plan && (
        <label className="inline-flex items-center gap-1.5 min-h-8 text-xs text-slate-600 dark:text-slate-300" title={row.coverage && !weekSelectable(row.coverage) ? `Already on a shopping list: ${coverageSummary(row.coverage)}` : 'Select for a shopping list'}>
          <input type="checkbox" className="w-5 h-5" aria-label={`Select week of ${row.weekStart} for a shopping list`} disabled={!row.coverage || !weekSelectable(row.coverage)}
            checked={selectedPlans.includes(row.plan.plan_id)} onChange={() => togglePlan(row.plan.plan_id)} />
        </label>
      )}
      {row.plan && row.coverage && <CoverageBadge coverage={row.coverage} onOpenTrip={openTrip} />}
    </>
  );

  if (!loaded && loading) {
    return (
      <div className="min-h-screen bg-slate-50 dark:bg-slate-950 flex items-center justify-center">
        <div className="animate-pulse flex flex-col items-center gap-4">
          <div className="h-12 w-12 bg-slate-200 dark:bg-slate-800 rounded-full"></div>
          <div className="text-slate-400 font-medium">Loading Meal Plan...</div>
        </div>
      </div>
    );
  }

  if (error && !loaded) {
    return (
      <div className="min-h-screen bg-slate-50 dark:bg-slate-950 flex items-center justify-center p-4">
        <div className="bg-white dark:bg-slate-900 p-8 rounded-xl shadow-lg border border-red-100 dark:border-red-900/30 max-w-md text-center">
          <div className="text-red-500 text-4xl mb-4">⚠️</div>
          <h1 className="text-xl font-bold text-slate-800 dark:text-slate-100 mb-2">Error Loading Plan</h1>
          <p className="text-slate-600 dark:text-slate-400">{error}</p>
          <button
            onClick={() => loadData(true)}
            className="mt-6 px-6 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded-lg hover:bg-slate-800 dark:hover:bg-slate-600 transition-colors"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  const actualRange = formatWeekRange(weekStart);
  const publicationDate = sourceContext?.exported_at ? `Snapshot as of ${formatLocalDateTime(sourceContext.exported_at)}` : 'Home plan';
  const shown = review?.plan ?? plan;
  const shownShopping = review?.shopping_list ?? shoppingList;
  const highlightIds = review ? new Set(review.changes.filter(change => change.kind !== 'removed').map(change => change.assignment_id)) : null;
  const thisWeek = mondayOf(todayIn(plan?.timezone));
  const emptyCopy = reader.mode === 'static' ? 'No published plan for this week.' : 'No plan for this week yet.';

  return (
    <div className="min-h-screen bg-slate-50 dark:bg-slate-950 text-slate-900 dark:text-slate-100 font-sans pb-20 transition-colors duration-200">
      {/* Header */}
      <header className="bg-white dark:bg-slate-900 border-b border-slate-200 dark:border-slate-800 sticky top-0 z-30 shadow-sm transition-colors duration-200">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="p-2 bg-indigo-600 rounded-lg text-white">
              <Utensils size={20} />
            </div>
            <div>
              <h1 className="text-lg font-bold leading-none text-slate-900 dark:text-white">Meal Prep</h1>
              <p className="text-xs text-slate-500 dark:text-slate-400 mt-1">{view === 'month' ? formatMonth(month) : actualRange}</p>
            </div>
          </div>
          <div className="flex items-center gap-2 sm:gap-4">
            <div className="text-xs text-slate-400 font-mono hidden sm:block">
              {publicationDate}{plan && <><br />Seed: {plan.seed}<br /><span className="text-[10px] opacity-75">r{plan.revision} · {formatLocalDateTime(plan.generated_at)}</span></>}
              <br />
              <span className="text-[10px] opacity-75">Garbage Collector: {garbageCollector}</span>
            </div>
            <button onClick={() => (view !== 'week' ? reloadMonth(true) : reload(true))} disabled={refreshing} className="px-3 py-2 rounded-lg bg-slate-100 dark:bg-slate-800 text-sm">{refreshing ? 'Refreshing…' : 'Refresh'}</button>
            {LoginDialog && (auth.username ? (
              <button onClick={logout} disabled={loggingOut} aria-label={`Log out (signed in as ${auth.username})`} title={`Signed in as ${auth.username}`}
                className="min-h-11 min-w-11 px-2 inline-flex items-center justify-center gap-2 rounded-lg bg-slate-100 dark:bg-slate-800 text-sm disabled:opacity-60">
                <LogOut size={18} /><span className="hidden sm:inline">{loggingOut ? 'Logging out…' : `Log out · ${auth.username}`}</span>
              </button>
            ) : (
              <button onClick={openLogin} aria-label="Log in" className="min-h-11 min-w-11 px-2 inline-flex items-center justify-center gap-2 rounded-lg bg-slate-100 dark:bg-slate-800 text-sm">
                <LogIn size={18} /><span className="hidden sm:inline">Log in</span>
              </button>
            ))}
            <button
              onClick={toggleTheme}
              className="p-2 text-slate-500 hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-200 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors"
              aria-label="Toggle theme"
            >
              {theme === 'dark' ? <Sun size={20} /> : <Moon size={20} />}
            </button>
          </div>
        </div>
      </header>

      {logoutError && <div role="alert" className="max-w-7xl mx-auto mt-4 px-4 py-3 bg-red-50 text-red-800 rounded-lg">{logoutError}</div>}
      {error && (plan || view !== 'week') && <div role="alert" className="max-w-7xl mx-auto mt-4 px-4 py-3 bg-red-50 text-red-800 rounded-lg">Refresh failed: {error} {view === 'week' ? 'The displayed plan and groceries are still from the same snapshot.' : 'What is shown is from the last successful load.'}</div>}

      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-6">
        <div role="group" aria-label="View" className="inline-flex rounded-lg border border-slate-200 dark:border-slate-800 overflow-hidden text-sm">
          {['week', 'month', 'shopping', ...(canPublish && PublishView ? ['publish'] : [])].map(name => (
            <button key={name} aria-pressed={view === name} disabled={!!review && name !== 'week'} onClick={() => (name === 'month' ? showMonth() : name === 'shopping' ? showShopping() : name === 'publish' ? showPublish() : openWeek(weekStart))}
              className={`px-4 min-h-11 capitalize ${view === name ? 'bg-indigo-600 text-white' : 'bg-white dark:bg-slate-900 text-slate-700 dark:text-slate-200'} disabled:opacity-50`}>{name}</button>
          ))}
        </div>

        {view === 'month' && newListHint && canEditTrips && selectedPlans.length === 0 && (
          <p role="status" className="mb-3 rounded-lg border border-indigo-200 dark:border-indigo-900/50 bg-indigo-50 dark:bg-indigo-950/30 px-3 py-2 text-sm text-indigo-900 dark:text-indigo-100">New shopping list: tick the weeks you want to shop for below, then tap Create shopping list.</p>
        )}
        {view === 'month' && (
          <MonthView reader={reader} month={month} onMonth={setMonth} thisMonth={monthOf(todayIn(plan?.timezone))} nonce={monthNonce}
            canEdit={canEdit} MonthFill={MonthFill} onOpenWeek={openWeek} onReload={() => reloadMonth()} renderWeekAccessory={renderWeekAccessory} onCoverage={setMonthCoverage} />
        )}
        {view === 'month' && canEditTrips && CreateTripBar && (
          <Suspense fallback={null}>
            <CreateTripBar planIds={selectedPlans} coverage={monthCoverage} reader={reader} onClear={() => setSelectedPlans([])}
              onCreated={trip => { setSelectedPlans([]); setMonthNonce(value => value + 1); openTrip(trip.trip_id); }}
              onProblem={() => { setSelectedPlans([]); setMonthNonce(value => value + 1); }} />
          </Suspense>
        )}

        {view === 'publish' && canPublish && PublishView && (
          <Suspense fallback={<p className="text-sm text-slate-500">Loading…</p>}>
            <PublishView today={todayIn(plan?.timezone)} />
          </Suspense>
        )}

        {view === 'shopping' && (
          <TripsView reader={reader} tripId={tripId} onSelect={setTripId} nonce={monthNonce} canEdit={canEditTrips} TripControls={TripControls}
            onOpenWeek={openWeek} onChanged={() => setTripNonce(value => value + 1)} onNewList={() => { setSelectedPlans([]); showMonth(); setNewListHint(true); }} />
        )}

        {view === 'week' && <>
        <nav aria-label="Week navigation" className="flex flex-wrap items-center gap-2">
          <button onClick={() => goToWeek(addDays(weekStart, -7))} disabled={!!review} aria-label="Previous week" className="p-2 rounded-lg bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 disabled:opacity-50"><ChevronLeft size={16} /></button>
          <button onClick={() => goToWeek(thisWeek)} disabled={!!review || weekStart === thisWeek} className="px-3 py-2 rounded-lg bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 text-sm disabled:opacity-50">This week</button>
          <button onClick={() => goToWeek(addDays(weekStart, 7))} disabled={!!review} aria-label="Next week" className="p-2 rounded-lg bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 disabled:opacity-50"><ChevronRight size={16} /></button>
          <span className="text-sm font-medium text-slate-600 dark:text-slate-300 ml-2">{actualRange}</span>
          {plan && !review && <WeekCoverage reader={reader} plan={plan} nonce={tripNonce + monthNonce} onOpenTrip={openTrip} />}
        </nav>

        {canEdit && EditingPanel && loaded && (
          <Suspense fallback={null}>
            <EditingPanel key={weekStart} weekStart={weekStart} plan={plan} review={review} onReview={setReview} onReload={reload} />
          </Suspense>
        )}

        {!shown ? (
          <div className="rounded-xl border border-dashed border-slate-300 dark:border-slate-700 p-10 text-center text-slate-600 dark:text-slate-300">
            {loading ? 'Loading week…' : error ? (
              <>Unable to load this week: {error} <button className="ml-2 underline" onClick={() => loadData(true)}>Retry</button></>
            ) : emptyCopy}
          </div>
        ) : (
        <div className="grid grid-cols-1 lg:grid-cols-4 gap-8">

          {/* Meal Grid (Takes up 3 columns on large screens) */}
          <div className="lg:col-span-3 space-y-8">
            <div className="flex items-center gap-2 mb-6">
              <Calendar className="text-indigo-600 dark:text-indigo-400" />
              <h2 className="text-xl font-bold text-slate-800 dark:text-white">{review ? 'Proposed Plan' : 'Weekly Plan'}</h2>
            </div>

            {swapNotice && swapNotice.revision === plan?.revision && (
              <div role="status" className="flex items-start justify-between gap-3 rounded-lg border border-emerald-200 dark:border-emerald-900/50 bg-emerald-50 dark:bg-emerald-950/30 text-emerald-900 dark:text-emerald-100 px-4 py-3 text-sm">
                <div className="space-y-1">
                  <p>{swapNotice.text} Use "Undo last change" above to revert.</p>
                  {swapNotice.warnings.length > 0 && (
                    <div className="text-amber-800 dark:text-amber-200">
                      <p>Heads-up: this week now breaks some planning rules (informational only):</p>
                      <ul className="list-disc pl-5">{swapNotice.warnings.map(w => <li key={`${w.rule}:${w.message}`}>{w.message}</li>)}</ul>
                    </div>
                  )}
                </div>
                <button className="min-h-8 px-2 underline" onClick={() => setSwapNotice(null)}>Dismiss</button>
              </div>
            )}

            <WeekGrid plan={shown} onSelect={setSelectedSlot} highlightIds={highlightIds}
              onSwap={canEdit && SwapDialog && !review ? slot => { setSwapNotice(null); setSwapSlot(slot); } : null} today={todayIn(plan?.timezone)} />

            {/* Stats Summary */}
            <div className="mt-12 bg-white dark:bg-slate-900 rounded-xl p-6 border border-slate-200 dark:border-slate-800 transition-colors duration-200">
              <h3 className="font-bold text-slate-800 dark:text-white mb-4">Distribution Stats</h3>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-6">
                <div>
                  <h4 className="text-xs uppercase text-slate-500 dark:text-slate-400 mb-2">Protein</h4>
                  <ul className="space-y-1 text-sm">
                    {Object.entries(shown.derived.protein_counts).map(([key, count]) => (
                      <li key={key} className="flex justify-between text-slate-700 dark:text-slate-300">
                        <span className="capitalize">{key}</span>
                        <span className="font-mono font-bold text-slate-900 dark:text-white">{count}</span>
                      </li>
                    ))}
                  </ul>
                </div>
                <div>
                  <h4 className="text-xs uppercase text-slate-500 dark:text-slate-400 mb-2">Carbs</h4>
                  <ul className="space-y-1 text-sm">
                    {Object.entries(shown.derived.carb_counts).map(([key, count]) => (
                      <li key={key} className="flex justify-between text-slate-700 dark:text-slate-300">
                        <span className="capitalize">{key}</span>
                        <span className="font-mono font-bold text-slate-900 dark:text-white">{count}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            </div>
          </div>

          {/* Shopping List Sidebar */}
          <div className="lg:col-span-1 h-fit sticky top-24">
            <ShoppingList items={shownShopping} plan={shown} mode={review ? `proposal-${review.proposal_id}` : reader.mode} />
          </div>
        </div>
        )}
        </>}
      </main>

      {/* Recipe Modal */}
      {selectedSlot && (
        <RecipeModal
          slot={selectedSlot}
          onClose={() => setSelectedSlot(null)}
          onSwap={canEdit && SwapDialog && !review && selectedSlot.date >= todayIn(plan?.timezone) ? () => { setSwapSlot(selectedSlot); setSelectedSlot(null); setSwapNotice(null); } : null}
        />
      )}
      {auth.loginOpen && LoginDialog && (
        <Suspense fallback={null}>
          <LoginDialog expired={auth.reason === 'expired'} onClose={closeLogin} onLoggedIn={username => authStore.dispatch({ type: 'logged_in', username })} />
        </Suspense>
      )}
      {swapSlot && SwapDialog && plan && (
        <Suspense fallback={null}>
          <SwapDialog slot={swapSlot} plan={plan} onClose={() => setSwapSlot(null)} onReload={() => loadData()}
            onApplied={async ({ variant, ruleWarnings, revision }) => {
              const name = variant.name;
              setSwapSlot(null);
              await loadData();
              setSwapNotice({ text: `Swapped to ${name}.`, warnings: ruleWarnings, revision });
              setTripNonce(value => value + 1);
            }} />
        </Suspense>
      )}
    </div>
  );
}

export default App;
