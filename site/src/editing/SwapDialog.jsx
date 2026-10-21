import { useEffect, useMemo, useRef, useState } from 'react';
import { X, Search, Check, ArrowRight, TriangleAlert } from 'lucide-react';
import { createPlanEditor } from './planEditor.js';
import { filterVariants, proteinOptions, groupRecipes, defaultVariant, carbLabel, isLinked, SCOPE_LABELS, groupGroceryDelta, tripImpactMessage, SWAP_WARNING_TITLE, swapErrorMessage, swapNeedsReload } from './swapModel.js';

const API_BASE = import.meta.env.VITE_API_BASE_PATH || '/api/v1';
const btn = 'min-h-11 px-4 py-2 rounded-lg text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed';
const btnPrimary = `${btn} bg-indigo-600 text-white hover:bg-indigo-500`;
const btnQuiet = `${btn} bg-slate-100 dark:bg-slate-800 text-slate-800 dark:text-slate-100 hover:bg-slate-200 dark:hover:bg-slate-700`;
const mealOf = change => `${change.date} ${change.meal}`;

// Catalog is stable for the page lifetime; fetch once.
let catalogPromise = null;

export default function SwapDialog({ slot, plan, onClose, onApplied, onReload }) {
  const editor = useMemo(() => createPlanEditor(API_BASE), []);
  const linked = isLinked(slot);
  const [variants, setVariants] = useState(null);
  const [query, setQuery] = useState('');
  const [protein, setProtein] = useState(null);
  const [scope, setScope] = useState('pair');
  const [recipe, setRecipe] = useState(null);
  const [chosen, setChosen] = useState(null);
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState(null);
  const [problem, setProblem] = useState(null);
  const generation = useRef(0);

  useEffect(() => {
    let live = true;
    catalogPromise ??= editor.getCatalogVariants();
    catalogPromise.then(items => { if (live) setVariants(items); })
      .catch(err => { catalogPromise = null; if (live) setProblem(swapErrorMessage(err)); });
    return () => { live = false; };
  }, [editor]);

  useEffect(() => {
    const onKey = event => { if (event.key === 'Escape' && !busy) onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [busy, onClose]);

  const request = (variant, nextScope) => ({ slotId: slot.assignment_id, variantId: variant.variant_id, scope: nextScope, expectedRevision: plan.revision });

  const loadPreview = async (variant, nextScope) => {
    const mine = ++generation.current;
    setChosen(variant); setPreview(null); setProblem(null); setBusy('preview');
    try {
      const result = await editor.previewSwap(plan.plan_id, request(variant, nextScope));
      if (mine === generation.current) setPreview(result);
    } catch (err) {
      if (mine !== generation.current) return;
      setProblem(swapErrorMessage(err));
      if (swapNeedsReload(err)) void onReload();
    } finally { if (mine === generation.current) setBusy(null); }
  };

  const changeScope = next => { setScope(next); if (chosen) void loadPreview(chosen, next); };
  const back = () => { generation.current += 1; setRecipe(null); setChosen(null); setPreview(null); setProblem(null); setBusy(null); };

  const confirm = async () => {
    if (!chosen || !preview || busy) return;
    setBusy('apply'); setProblem(null);
    try {
      const result = await editor.applySwap(plan.plan_id, request(chosen, scope));
      await onApplied({ variant: chosen, ruleWarnings: result.ruleWarnings, revision: result.plan.revision });
    } catch (err) {
      setProblem(swapErrorMessage(err));
      if (swapNeedsReload(err)) { await onReload(); back(); setProblem(swapErrorMessage(err)); }
    } finally { setBusy(null); }
  };

  const current = useMemo(() => variants?.find(item => item.variant_id === slot.variant_id) ?? null, [variants, slot.variant_id]);
  const shown = useMemo(() => {
    if (!variants) return [];
    const matching = new Set(filterVariants(variants, query, protein).map(item => item.recipe_id));
    return groupRecipes(variants).filter(item => matching.has(item.recipe_id));
  }, [variants, query, protein]);
  const pickRecipe = item => { setRecipe(item); void loadPreview(defaultVariant(item, current?.carb), scope); };
  const groups = preview ? groupGroceryDelta(preview.grocery_delta) : [];
  const impact = preview ? tripImpactMessage(preview.trip) : null;

  return (
    <div className="fixed inset-0 z-50 flex items-stretch sm:items-center justify-center sm:p-4 bg-black/50 backdrop-blur-sm">
      <div role="dialog" aria-modal="true" aria-label="Swap meal" className="bg-white dark:bg-slate-900 w-full sm:max-w-lg sm:rounded-2xl shadow-xl flex flex-col h-dvh sm:h-auto sm:max-h-[90vh] overflow-hidden">
        <div className="flex items-start justify-between gap-3 px-4 pt-4 pb-3 border-b border-slate-100 dark:border-slate-800">
          <div className="min-w-0">
            <p className="text-xs uppercase tracking-wider font-semibold text-slate-500 dark:text-slate-400">Swap {slot.day} {slot.meal} · {slot.date}</p>
            <h2 className="text-lg font-bold text-slate-900 dark:text-white truncate">Currently: {slot.recipe_name}</h2>
          </div>
          <button aria-label="Close" onClick={onClose} disabled={busy === 'apply'} className="min-w-11 min-h-11 flex items-center justify-center rounded-full hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-500"><X size={20} /></button>
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-3 space-y-4">
          {linked && (
            <fieldset className="space-y-2">
              <legend className="text-sm font-semibold text-slate-800 dark:text-slate-100 mb-1">This meal is linked to its {slot.meal === 'lunch' ? 'dinner' : 'lunch'}</legend>
              {['pair', 'single'].map(value => (
                <label key={value} className="flex items-center gap-3 min-h-11 text-sm text-slate-700 dark:text-slate-200 cursor-pointer">
                  <input type="radio" name="swap-scope" className="w-5 h-5" checked={scope === value} disabled={busy === 'apply'} onChange={() => changeScope(value)} />
                  {SCOPE_LABELS[value]}
                </label>
              ))}
            </fieldset>
          )}

          {problem && <div role="alert" className="rounded-lg border border-red-200 dark:border-red-900/50 bg-red-50 dark:bg-red-950/30 text-red-800 dark:text-red-200 px-3 py-2 text-sm">{problem}</div>}

          {!recipe && (
            <>
              <div className="relative">
                <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
                <input type="search" aria-label="Search recipes" placeholder="Search recipes" value={query} onChange={event => setQuery(event.target.value)}
                  className="w-full min-h-11 pl-9 pr-3 rounded-lg border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-950 text-base" />
              </div>
              {variants && (
                <div role="group" aria-label="Filter by protein" className="flex flex-wrap gap-2">
                  {[null, ...proteinOptions(variants)].map(value => (
                    <button key={value ?? 'all'} aria-pressed={protein === value} onClick={() => setProtein(value)}
                      className={`min-h-11 px-3 rounded-full text-sm capitalize border ${protein === value ? 'bg-indigo-600 text-white border-indigo-600' : 'bg-white dark:bg-slate-900 border-slate-300 dark:border-slate-700 text-slate-700 dark:text-slate-200'}`}>{value ?? 'All'}</button>
                  ))}
                </div>
              )}
              {!variants && !problem && <p role="status" className="text-sm text-slate-500">Loading recipes…</p>}
              {variants && shown.length === 0 && <p className="text-sm text-slate-500">No recipes match.</p>}
              <ul className="divide-y divide-slate-100 dark:divide-slate-800" aria-label="Recipes">
                {shown.map(item => {
                  const isCurrent = item.recipe_id === current?.recipe_id;
                  return (
                    <li key={item.recipe_id}>
                      <button disabled={busy !== null} onClick={() => pickRecipe(item)} className="w-full min-h-14 py-2 px-1 text-left flex items-center gap-3 hover:bg-slate-50 dark:hover:bg-slate-800/60 disabled:opacity-60">
                        <span className="flex-1 min-w-0">
                          <span className="block font-medium text-slate-900 dark:text-white">{item.name}</span>
                          <span className="block text-xs text-slate-500 dark:text-slate-400"><span className="capitalize">{item.protein}</span> · {item.variants.length === 1 ? carbLabel(item.variants[0].carb) : `${item.variants.length} carb options`}</span>
                        </span>
                        {isCurrent && <span className="text-xs font-semibold text-indigo-600 dark:text-indigo-300 inline-flex items-center gap-1"><Check size={14} />Current</span>}
                      </button>
                    </li>
                  );
                })}
              </ul>
            </>
          )}

          {recipe && (
            <section aria-label="Swap preview" className="space-y-4 text-sm">
              <p className="font-semibold text-slate-800 dark:text-slate-100">New recipe: {recipe.name}</p>
              {recipe.variants.length > 1 && (
                <div role="radiogroup" aria-label="Carb" className="flex flex-wrap gap-2">
                  {recipe.variants.map(item => {
                    const selected = item.variant_id === chosen?.variant_id;
                    return (
                      <button key={item.variant_id} role="radio" aria-checked={selected} disabled={busy === 'apply'}
                        onClick={() => { if (!selected) void loadPreview(item, scope); }}
                        className={`min-h-11 px-3 rounded-full text-sm border ${selected ? 'bg-indigo-600 text-white border-indigo-600' : 'bg-white dark:bg-slate-900 border-slate-300 dark:border-slate-700 text-slate-700 dark:text-slate-200'}`}>
                        {carbLabel(item.carb)}{item.variant_id === slot.variant_id ? ' (current)' : ''}
                      </button>
                    );
                  })}
                </div>
              )}
              {busy === 'preview' && <p role="status" className="text-slate-500">Checking the effect on this week…</p>}
              {preview && (
                <>
                  {preview.changes.length === 0 && <p className="text-slate-600 dark:text-slate-300">Nothing would change.</p>}
                  {preview.changes.length > 0 && (
                    <ul aria-label="Meal changes" className="space-y-1">
                      {preview.changes.map(change => (
                        <li key={`${change.assignment_id}-${change.kind}`} className="text-slate-700 dark:text-slate-300">
                          <span className="font-mono text-xs uppercase mr-2">{mealOf(change)}</span>
                          <span className="line-through opacity-70">{change.before?.recipe_name ?? '(none)'}</span>
                          <ArrowRight size={12} className="inline mx-1" />
                          <span className="font-semibold">{change.after?.recipe_name ?? '(removed)'}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                  <div>
                    <h3 className="font-semibold text-slate-800 dark:text-slate-100 mb-1">Groceries for this week</h3>
                    {groups.length === 0 ? <p className="text-slate-600 dark:text-slate-300">No change to the shopping list.</p> : (
                      <div className="space-y-2">
                        {groups.map(group => (
                          <div key={group.section}>
                            <p className="text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400">{group.section}</p>
                            <ul className="grid grid-cols-1 gap-x-4">
                              {group.items.map(item => (
                                <li key={`${item.ingredient_id}-${item.unit}`} className="flex justify-between gap-3">
                                  <span className="text-slate-700 dark:text-slate-300">{item.display}</span>
                                  <span className={`font-mono ${item.direction === 'more' ? 'text-emerald-700 dark:text-emerald-400' : 'text-rose-700 dark:text-rose-400'}`}>{item.text}</span>
                                </li>
                              ))}
                            </ul>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                  {preview.rule_warnings.length > 0 && (
                    <div role="note" className="rounded-lg border border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/30 text-amber-900 dark:text-amber-100 px-3 py-2 space-y-1">
                      <p className="font-semibold"><TriangleAlert size={14} className="inline mr-1" />{SWAP_WARNING_TITLE}</p>
                      <ul className="list-disc pl-5">{preview.rule_warnings.map((warning, index) => <li key={index}>{warning.message}</li>)}</ul>
                      <p className="text-xs opacity-80">You can still swap. These rules only guide generated plans.</p>
                    </div>
                  )}
                  {impact && (
                    <p className={`rounded-lg px-3 py-2 ${impact.kind === 'done' ? 'bg-amber-50 dark:bg-amber-950/30 text-amber-900 dark:text-amber-100 border border-amber-300 dark:border-amber-800' : 'bg-slate-50 dark:bg-slate-800 text-slate-700 dark:text-slate-200'}`}>{impact.text}</p>
                  )}
                </>
              )}
            </section>
          )}
        </div>

        <div className="px-4 py-3 border-t border-slate-100 dark:border-slate-800 bg-slate-50 dark:bg-slate-800/50 flex gap-2 justify-end">
          {recipe && <button className={btnQuiet} disabled={busy === 'apply'} onClick={back}>Back</button>}
          <button className={btnQuiet} disabled={busy === 'apply'} onClick={onClose}>Cancel</button>
          {recipe && <button className={btnPrimary} disabled={!preview || busy !== null} onClick={confirm}>{busy === 'apply' ? 'Swapping…' : 'Swap'}</button>}
        </div>
      </div>
    </div>
  );
}
