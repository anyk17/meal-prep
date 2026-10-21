import { useEffect, useState } from 'react';
import { cn } from '../lib/utils';
import { COVERAGE_LABEL } from '../tripModel.js';

const STYLE = {
  not_covered: 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300',
  on_list: 'bg-indigo-100 text-indigo-800 dark:bg-indigo-900/40 dark:text-indigo-200',
  covered: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300',
};

// coverage is a WeekCoverage entry (optionally decorated with trip_name); null while unknown, so nothing is claimed.
export function CoverageBadge({ coverage, onOpenTrip }) {
  if (!coverage) return null;
  const label = COVERAGE_LABEL[coverage.coverage];
  const base = cn('inline-flex items-center text-[11px] font-medium px-2 py-0.5 rounded-full', STYLE[coverage.coverage]);
  const title = coverage.trip_name ? `${label}: ${coverage.trip_name}` : label;
  return (
    <span className="inline-flex flex-wrap items-center gap-1" data-coverage={coverage.coverage}>
      {coverage.trip_id && onOpenTrip ? (
        <button type="button" onClick={() => onOpenTrip(coverage.trip_id)} title={`Open ${title}`} className={cn(base, 'hover:underline min-h-6')}>{label}</button>
      ) : <span className={base} title={title}>{label}</span>}
      {coverage.changed_since_shopping && <span className="text-[11px] px-2 py-0.5 rounded-full bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200" title="The plan was edited after this list was marked done">changed since shopping</span>}
    </span>
  );
}

// Coverage of the single week shown in the week view. Reads through the shared reader, so it works in both modes.
export function WeekCoverage({ reader, plan, nonce, onOpenTrip }) {
  const [state, setState] = useState({ key: null, coverage: null });
  const key = `${plan.plan_id}:${plan.revision}:${nonce}:${reader.mode}`;
  useEffect(() => {
    let live = true;
    Promise.all([reader.getCoverage({ from: plan.week_start, to: plan.week_end }), reader.listShoppingTrips().catch(() => null)])
      .then(([coverage, trips]) => {
        if (!live) return;
        const entry = coverage.items.find(item => item.plan_id === plan.plan_id) ?? null;
        const name = entry && trips?.items.find(trip => trip.trip_id === entry.trip_id)?.name;
        setState({ key, coverage: entry && { ...entry, trip_name: name ?? null } });
      })
      .catch(() => { if (live) setState({ key, coverage: null }); });
    return () => { live = false; };
  }, [reader, plan.plan_id, plan.week_start, plan.week_end, key]);
  return state.key === key ? <CoverageBadge coverage={state.coverage} onOpenTrip={onOpenTrip} /> : null;
}
