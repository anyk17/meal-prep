import { useState, useEffect, useRef, useCallback, lazy, Suspense } from 'react';
import { MealCard } from './components/MealCard';
import { ShoppingList } from './components/ShoppingList';
import { RecipeModal } from './components/RecipeModal';
import { WeekGrid } from './components/WeekGrid';
import { Utensils, Calendar, Moon, Sun, ChevronLeft, ChevronRight } from 'lucide-react';
import { createApiReader } from './readers/api';
import { createStaticReader } from './readers/static';
import { validatePair, addDays } from './readers/shared.js';
import { mondayOf, todayIn, formatWeekRange } from './dates.js';

const createReader = () => import.meta.env.VITE_READER_MODE === 'static'
  ? createStaticReader('publication/index.json')
  : createApiReader(import.meta.env.VITE_API_BASE_PATH || '/api/v1');
let reader = createReader();
const INITIAL_WEEK = mondayOf(import.meta.env.VITE_DEFAULT_FROM || '2026-10-19');

// Editing is home-only. In the static (Pages) build these constants fold to null at build time, so
// the write client and editing UI are never bundled and cannot be enabled at runtime.
const loadEditor = import.meta.env.VITE_READER_MODE === 'static' ? null : () => import('./editing/planEditor.js');
const EditingPanel = import.meta.env.VITE_READER_MODE === 'static' ? null : lazy(() => import('./editing/EditingPanel.jsx'));

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
  const [review, setReview] = useState(null);
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
    try { const { createPlanEditor } = await loadEditor(); setCanEdit((await createPlanEditor(import.meta.env.VITE_API_BASE_PATH || '/api/v1').getCapabilities()).canEditPlans); }
    catch { setCanEdit(false); }
  }, []);
  useEffect(() => { void refreshCapabilities(); }, [refreshCapabilities]);

  const goToWeek = target => {
    if (target === weekStart) return;
    hasPlan.current = false;
    setReview(null); setPlan(null); setShoppingList(null); setError(null); setSelectedSlot(null);
    setWeekStart(target);
  };
  const reload = async (refresh = false) => { if (refresh) void refreshCapabilities(); await loadData(refresh); };

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
              <p className="text-xs text-slate-500 dark:text-slate-400 mt-1">{actualRange}</p>
            </div>
          </div>
          <div className="flex items-center gap-4">
            <div className="text-xs text-slate-400 font-mono hidden sm:block">
              {publicationDate}{plan && <><br />Seed: {plan.seed}<br /><span className="text-[10px] opacity-75">r{plan.revision} · {formatLocalDateTime(plan.generated_at)}</span></>}
              <br />
              <span className="text-[10px] opacity-75">Garbage Collector: {garbageCollector}</span>
            </div>
            <button onClick={() => reload(true)} disabled={refreshing} className="px-3 py-2 rounded-lg bg-slate-100 dark:bg-slate-800 text-sm">{refreshing ? 'Refreshing…' : 'Refresh'}</button>
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

      {error && plan && <div role="alert" className="max-w-7xl mx-auto mt-4 px-4 py-3 bg-red-50 text-red-800 rounded-lg">Refresh failed: {error} The displayed plan and groceries are still from the same snapshot.</div>}

      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-6">
        <nav aria-label="Week navigation" className="flex flex-wrap items-center gap-2">
          <button onClick={() => goToWeek(addDays(weekStart, -7))} disabled={!!review} aria-label="Previous week" className="p-2 rounded-lg bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 disabled:opacity-50"><ChevronLeft size={16} /></button>
          <button onClick={() => goToWeek(thisWeek)} disabled={!!review || weekStart === thisWeek} className="px-3 py-2 rounded-lg bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 text-sm disabled:opacity-50">This week</button>
          <button onClick={() => goToWeek(addDays(weekStart, 7))} disabled={!!review} aria-label="Next week" className="p-2 rounded-lg bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 disabled:opacity-50"><ChevronRight size={16} /></button>
          <span className="text-sm font-medium text-slate-600 dark:text-slate-300 ml-2">{actualRange}</span>
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

            <WeekGrid plan={shown} onSelect={setSelectedSlot} highlightIds={highlightIds} />

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
      </main>

      {/* Recipe Modal */}
      {selectedSlot && (
        <RecipeModal
          slot={selectedSlot}
          onClose={() => setSelectedSlot(null)}
        />
      )}
    </div>
  );
}

export default App;
