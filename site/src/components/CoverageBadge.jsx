import { useEffect, useState } from 'react';
import { cn } from '../lib/utils';
import { COVERAGE_LABEL, SHELF_LIVES, coverageSummary, decorateCoverage, groupLabel } from '../tripModel.js';

const STYLE = {
  not_covered: 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300',
  to_buy: 'bg-indigo-100 text-indigo-800 dark:bg-indigo-900/40 dark:text-indigo-200',
  bought: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300',
};

// A decorated WeekCoverage entry ({ groups: { long_lasting: { coverage, trip_id, changed_since_shopping, trip_name }, fresh: {...} } });
// null while unknown, so nothing is claimed. Two compact pills, one per shelf-life group.
function GroupPill({ group, entry, onOpenTrip }) {
  const label = `${groupLabel(group)}: ${COVERAGE_LABEL[entry.coverage].toLowerCase()}`;
  const base = cn('inline-flex items-center text-[11px] font-medium px-2 py-0.5 rounded-full', STYLE[entry.coverage]);
  const title = entry.trip_name ? `${label} (${entry.trip_name})` : label;
  return (
    <span className="inline-flex flex-wrap items-center gap-1" data-group={group} data-coverage={entry.coverage}>
      {entry.trip_id && onOpenTrip ? (
        <button type="button" onClick={() => onOpenTrip(entry.trip_id)} title={`Open ${title}`} className={cn(base, 'hover:underline min-h-6')}>{label}</button>
      ) : <span className={base} title={title}>{label}</span>}
      {entry.changed_since_shopping && <span className="text-[11px] px-2 py-0.5 rounded-full bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200" title="The plan was edited after this list was marked bought">{groupLabel(group)} changed since shopping</span>}
    </span>
  );
}

export function CoverageBadge({ coverage, onOpenTrip }) {
  if (!coverage?.groups) return null;
  return (
    <span className="inline-flex flex-wrap items-center gap-1" aria-label={coverageSummary(coverage)}>
      {SHELF_LIVES.map(group => <GroupPill key={group} group={group} entry={coverage.groups[group]} onOpenTrip={onOpenTrip} />)}
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
        setState({ key, coverage: decorateCoverage(entry, trips?.items) });
      })
      .catch(() => { if (live) setState({ key, coverage: null }); });
    return () => { live = false; };
  }, [reader, plan.plan_id, plan.week_start, plan.week_end, key]);
  return state.key === key ? <CoverageBadge coverage={state.coverage} onOpenTrip={onOpenTrip} /> : null;
}
