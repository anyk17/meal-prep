import { useState, useEffect, useRef, useCallback } from 'react';
import { MealCard } from './components/MealCard';
import { ShoppingList } from './components/ShoppingList';
import { RecipeModal } from './components/RecipeModal';
import { Utensils, Calendar, Moon, Sun } from 'lucide-react';
import { createApiReader } from './readers/api';
import { createStaticReader } from './readers/static';
import { validatePair } from './readers/shared.js';

const createReader = () => import.meta.env.VITE_READER_MODE === 'static'
  ? createStaticReader('publication/index.json')
  : createApiReader(import.meta.env.VITE_API_BASE_PATH || '/api/v1');
let reader = createReader();
const INITIAL_FROM = import.meta.env.VITE_DEFAULT_FROM || '2026-10-19';
const INITIAL_TO = import.meta.env.VITE_DEFAULT_TO || '2026-10-25';

// Map shorthand days to full names
const DAYS = {
  mon: 'Monday',
  tue: 'Tuesday',
  wed: 'Wednesday',
  thu: 'Thursday',
  fri: 'Friday',
  sat: 'Saturday',
  sun: 'Sunday'
};

const MEALS = ['lunch', 'dinner'];
const WEEKEND_DAYS = new Set(['sat', 'sun']);
const WEEKEND_COPY = {
  sat: {
    badge: 'Saturday',
    title: 'Eat outside',
    description: 'No cooking planned. Leave room for a lunch out, a casual dinner, or whatever sounds good that day.'
  },
  sun: {
    badge: 'Sunday',
    title: 'Kitchen off',
    description: 'Recovery day. Keep it flexible with leftovers, takeout, or a spontaneous plan before the next prep cycle.'
  }
};

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
      const range = { from: INITIAL_FROM, to: INITIAL_TO };
      const result = await candidateReader.listPlans(range);
      let selected = result.items.find(item => item.week_start === INITIAL_FROM) || result.items[0];
      if (generation !== requestGeneration.current) return;
      if (!selected) { reader = candidateReader; hasPlan.current=false; setPlan(null); setShoppingList(null); setSourceContext(candidateReader.context); setSelectedSlot(null); setError(null); return; }
      let nextPlan, nextShop;
      for (let attempt = 0; attempt < (candidateReader.mode === 'api' ? 2 : 1); attempt += 1) {
        nextPlan = await candidateReader.getPlan(selected.plan_id);
        try { nextShop = await candidateReader.getShoppingList(selected.plan_id, nextPlan.revision, nextPlan.snapshot_id); break; }
        catch (err) {
          if (err.code !== 'snapshot_changed' || attempt > 0 || candidateReader.mode !== 'api') throw err;
          const refreshed = await candidateReader.listPlans(range);
          selected = refreshed.items.find(item => item.plan_id === selected.plan_id) || refreshed.items.find(item => item.week_start === INITIAL_FROM) || refreshed.items[0];
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
    } catch (err) {
      if (generation !== requestGeneration.current) return;
      setError(err?.message || 'Could not load the meal plan.');
    } finally { if (generation === requestGeneration.current) { setLoading(false); setRefreshing(false); } }
  }, []);

  useEffect(() => { void loadData(); return () => { requestGeneration.current += 1; }; }, [loadData]);

  if (loading && !plan) {
    return (
      <div className="min-h-screen bg-slate-50 dark:bg-slate-950 flex items-center justify-center">
        <div className="animate-pulse flex flex-col items-center gap-4">
          <div className="h-12 w-12 bg-slate-200 dark:bg-slate-800 rounded-full"></div>
          <div className="text-slate-400 font-medium">Loading Meal Plan...</div>
        </div>
      </div>
    );
  }

  if (error && !plan) {
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

  if (!plan) return <div className="min-h-screen bg-slate-50 dark:bg-slate-950 p-8 text-center text-slate-700 dark:text-slate-200">{error ? `Unable to load plan: ${error}` : `${reader.mode === 'static' ? 'No published plan' : 'No plan'} for ${INITIAL_FROM}–${INITIAL_TO}.`}<button className="ml-4 underline" onClick={() => loadData(true)}>Refresh</button></div>;

  const actualRange = `${plan.week_start} – ${plan.week_end}`;
  const publicationDate = sourceContext?.exported_at ? `Snapshot as of ${formatLocalDateTime(sourceContext.exported_at)}` : 'Home plan';
  // Organize slots by day and meal
  const getSlot = (day, meal) => {
    return plan?.slots.find(s => s.day === day && s.meal === meal);
  };

  const isWeekend = (day) => WEEKEND_DAYS.has(day);

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
              {publicationDate}<br />Seed: {plan.seed}
              <br />
              <span className="text-[10px] opacity-75">
                {formatLocalDateTime(plan.generated_at)}
              </span>
              <br />
              <span className="text-[10px] opacity-75">Garbage Collector: {garbageCollector}</span>
            </div>
            <button onClick={() => loadData(true)} disabled={refreshing} className="px-3 py-2 rounded-lg bg-slate-100 dark:bg-slate-800 text-sm">{refreshing ? 'Refreshing…' : 'Refresh'}</button>
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

      {error && <div role="alert" className="max-w-7xl mx-auto mt-4 px-4 py-3 bg-red-50 text-red-800 rounded-lg">Refresh failed: {error} The displayed plan and groceries are still from the same snapshot.</div>}

      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <div className="grid grid-cols-1 lg:grid-cols-4 gap-8">

          {/* Meal Grid (Takes up 3 columns on large screens) */}
          <div className="lg:col-span-3 space-y-8">
            <div className="flex items-center gap-2 mb-6">
              <Calendar className="text-indigo-600 dark:text-indigo-400" />
              <h2 className="text-xl font-bold text-slate-800 dark:text-white">Weekly Plan</h2>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-6">
              {Object.keys(DAYS).map(day => (
                <div key={day} className="bg-white dark:bg-slate-900 rounded-xl shadow-sm border border-slate-200 dark:border-slate-800 overflow-hidden flex flex-col h-full transition-colors duration-200">
                  <div className="bg-slate-50 dark:bg-slate-800/50 px-4 py-3 border-b border-slate-100 dark:border-slate-800 flex items-center justify-between">
                    <span className="font-bold text-slate-700 dark:text-slate-200">{DAYS[day]}</span>
                    <span className="text-xs text-slate-400 uppercase font-mono">{day}</span>
                  </div>

                  <div className="p-4 space-y-4 flex-1 flex flex-col justify-center">
                    {isWeekend(day) ? (
                      <div className="flex-1 rounded-2xl border border-dashed border-amber-300/80 dark:border-amber-700/60 bg-gradient-to-br from-amber-50 via-orange-50 to-rose-50 dark:from-amber-950/40 dark:via-orange-950/30 dark:to-rose-950/30 p-5 flex flex-col justify-between min-h-[220px]">
                        <div className="flex items-center justify-between gap-3">
                          <span className="text-[11px] font-semibold uppercase tracking-[0.24em] text-amber-700 dark:text-amber-300">
                            {WEEKEND_COPY[day].badge}
                          </span>
                          <div className="rounded-full bg-white/80 dark:bg-slate-900/60 p-2 text-amber-500 dark:text-amber-300 shadow-sm">
                            <Sun size={18} />
                          </div>
                        </div>

                        <div className="space-y-3">
                          <h3 className="text-xl font-bold text-slate-900 dark:text-white">
                            {WEEKEND_COPY[day].title}
                          </h3>
                          <p className="text-sm leading-6 text-slate-600 dark:text-slate-300">
                            {WEEKEND_COPY[day].description}
                          </p>
                        </div>

                      </div>
                    ) : (
                      MEALS.map(meal => {
                        const dto = getSlot(day, meal);
                        if (!dto) return null;
                        const slot = { ...dto, recipeName: dto.recipe_name, proteinQty: dto.protein_quantity.value, proteinUnit: dto.protein_quantity.unit, carb: dto.carb ?? 'none', carbQty: dto.carb_quantity?.value, carbUnit: dto.carb_quantity?.unit, ingredients: dto.ingredients.map(i => ({ ...i, qty: i.quantity })) };

                        return (
                          <MealCard
                            key={`${day}-${meal}`}
                            slot={slot}
                            onClick={() => setSelectedSlot(slot)}
                          />
                        );
                      })
                    )}
                  </div>
                </div>
              ))}
            </div>

            {/* Stats Summary */}
            <div className="mt-12 bg-white dark:bg-slate-900 rounded-xl p-6 border border-slate-200 dark:border-slate-800 transition-colors duration-200">
              <h3 className="font-bold text-slate-800 dark:text-white mb-4">Distribution Stats</h3>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-6">
                <div>
                  <h4 className="text-xs uppercase text-slate-500 dark:text-slate-400 mb-2">Protein</h4>
                  <ul className="space-y-1 text-sm">
                    {Object.entries(plan.derived.protein_counts).map(([key, count]) => (
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
                    {Object.entries(plan.derived.carb_counts).map(([key, count]) => (
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
            <ShoppingList items={shoppingList} plan={plan} mode={reader.mode} />
          </div>
        </div>
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
