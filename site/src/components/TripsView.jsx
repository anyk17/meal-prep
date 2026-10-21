import { Suspense, useEffect, useState } from 'react';
import { ArrowLeft, Check, ShoppingCart } from 'lucide-react';
import { cn } from '../lib/utils';
import { formatWeekRange } from '../dates.js';
import { countChecked, formatQuantity, groupItems, itemCheckKey, readTripChecks, sortTrips, tripStorageKey, writeTripChecks } from '../tripModel.js';

const card = 'bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800';
const shortDate = value => new Intl.DateTimeFormat(undefined, { timeZone: 'UTC', month: 'short', day: 'numeric' }).format(new Date(`${value}T00:00:00Z`));
const formatDateTime = value => { const d = new Date(value); return Number.isNaN(d.getTime()) ? value : new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(d); };
const weeksLabel = trip => `${trip.weeks.length} ${trip.weeks.length === 1 ? 'week' : 'weeks'}`;
const rangeLabel = trip => { const last = trip.weeks.at(-1); return `${shortDate(trip.weeks[0].week_start)} – ${shortDate(last.week_end)}`; };

export function StatusChip({ status }) {
  return <span className={cn('text-xs font-medium px-2 py-0.5 rounded-full shrink-0', status === 'done' ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300' : 'bg-indigo-100 text-indigo-800 dark:bg-indigo-900/40 dark:text-indigo-200')}>{status === 'done' ? 'Done' : 'Open'}</span>;
}

function TripList({ trips, onOpen, staticMode, canEdit }) {
  if (!trips.length) {
    return (
      <div className="rounded-xl border border-dashed border-slate-300 dark:border-slate-700 p-8 text-center text-slate-600 dark:text-slate-300 space-y-2">
        <ShoppingCart className="mx-auto text-slate-400" />
        <p className="font-medium">{staticMode ? 'No shopping lists have been published yet.' : 'No shopping lists yet.'}</p>
        <p className="text-sm text-slate-500 dark:text-slate-400">{staticMode ? 'Check back after the next publication.' : canEdit ? 'Open the Month view, tick the weeks you want to shop for, and create a list.' : 'Shopping lists can be created from the Month view at home.'}</p>
      </div>
    );
  }
  return (
    <ul className="space-y-3">
      {sortTrips(trips).map(trip => (
        <li key={trip.trip_id}>
          <button type="button" onClick={() => onOpen(trip.trip_id)} className={cn(card, 'w-full text-left p-4 min-h-[4.5rem] flex items-center gap-3 hover:border-indigo-300 dark:hover:border-indigo-700')}>
            <div className="min-w-0 flex-1">
              <p className="font-semibold text-slate-900 dark:text-white truncate">{trip.name}</p>
              <p className="text-sm text-slate-500 dark:text-slate-400">{rangeLabel(trip)} · {weeksLabel(trip)} · {trip.items.length} items</p>
              {trip.weeks.some(week => week.changed_since_shopping) && <p className="text-xs text-amber-700 dark:text-amber-300 mt-0.5">A plan changed since shopping</p>}
            </div>
            <StatusChip status={trip.status} />
          </button>
        </li>
      ))}
    </ul>
  );
}

function TripDetail({ trip, mode, exportedAt, onBack, onOpenWeek, controls }) {
  const storageKey = tripStorageKey(mode, trip);
  const [state, setState] = useState(() => ({ key: storageKey, values: readTripChecks(storageKey) }));
  const checks = state.key === storageKey ? state.values : readTripChecks(storageKey);
  useEffect(() => { if (state.key === storageKey) writeTripChecks(storageKey, state.values); }, [state, storageKey]);
  const update = change => setState(previous => {
    const current = previous.key === storageKey ? previous.values : readTripChecks(storageKey);
    return { key: storageKey, values: change(current) };
  });
  const toggle = item => update(current => { const next = { ...current }; const key = itemCheckKey(item); if (next[key]) delete next[key]; else next[key] = true; return next; });
  const checked = countChecked(trip.items, checks);
  const sections = groupItems(trip.items, checks);
  const weekLabels = new Map(trip.weeks.map(week => [week.plan_id, shortDate(week.week_start)]));

  return (
    <div className="space-y-4">
      <button type="button" onClick={onBack} className="inline-flex items-center gap-1 min-h-11 text-sm text-indigo-700 dark:text-indigo-300"><ArrowLeft size={16} />All shopping lists</button>
      <header className={cn(card, 'p-4 space-y-3')}>
        <div className="flex items-start gap-3">
          <h2 className="text-xl font-bold text-slate-900 dark:text-white flex-1 min-w-0 break-words">{trip.name}</h2>
          <StatusChip status={trip.status} />
        </div>
        {mode === 'static' && exportedAt && <p className="text-xs text-slate-500 dark:text-slate-400">Snapshot as of {formatDateTime(exportedAt)}</p>}
        {trip.status === 'done' && trip.completed_at && <p className="text-xs text-slate-500 dark:text-slate-400">Shopping done {formatDateTime(trip.completed_at)}</p>}
        <ul aria-label="Weeks covered" className="flex flex-wrap gap-2">
          {trip.weeks.map(week => (
            <li key={week.plan_id} className="text-xs rounded-lg bg-slate-100 dark:bg-slate-800 px-2 py-1">
              <button type="button" className="hover:underline text-left" onClick={() => onOpenWeek(week.week_start)}>{formatWeekRange(week.week_start)}</button>
              <span className="font-mono text-slate-500 dark:text-slate-400"> r{week.revision}</span>
              {week.changed_since_shopping && <span className="text-amber-700 dark:text-amber-300"> · changed since shopping (now r{week.current_revision})</span>}
            </li>
          ))}
        </ul>
        {controls}
      </header>

      <div className="flex items-center justify-between sticky top-16 z-20 bg-slate-50/95 dark:bg-slate-950/95 backdrop-blur py-2" data-testid="trip-progress">
        <p className="text-sm text-slate-600 dark:text-slate-300">Checkmarks stay on this device.</p>
        <span className="text-sm font-semibold px-3 py-1 rounded-full bg-slate-900 text-white dark:bg-slate-700" aria-live="polite">{checked} of {trip.items.length}</span>
      </div>

      {sections.map(group => (
        <section key={group.section} aria-label={group.section} className={cn(card, 'overflow-hidden')}>
          <h3 className="px-4 py-2 text-xs font-semibold uppercase tracking-wider bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-200 flex justify-between"><span>{group.section}</span><span className="font-mono normal-case">{group.checked}/{group.total}</span></h3>
          <ul className="divide-y divide-slate-100 dark:divide-slate-800">
            {group.items.map(item => {
              const on = !!checks[itemCheckKey(item)];
              const partial = trip.weeks.length > 1 && item.plan_ids.length < trip.weeks.length;
              return (
                <li key={itemCheckKey(item)}>
                  <button type="button" role="checkbox" aria-checked={on} onClick={() => toggle(item)}
                    className={cn('w-full min-h-14 px-4 py-3 flex items-center gap-4 text-left select-none transition-opacity', on && 'opacity-50 bg-slate-50 dark:bg-slate-900/60')}>
                    <span className={cn('w-7 h-7 rounded-md border-2 flex items-center justify-center shrink-0', on ? 'bg-green-500 border-green-500 text-white' : 'border-slate-300 dark:border-slate-600')}>{on && <Check size={18} strokeWidth={3} />}</span>
                    <span className="flex-1 min-w-0">
                      <span className={cn('block text-base font-medium text-slate-900 dark:text-slate-100', on && 'line-through')}>{item.display}</span>
                      {partial && <span className="block text-xs text-slate-500 dark:text-slate-400">{item.plan_ids.map(id => weekLabels.get(id)).join(', ')}</span>}
                    </span>
                    <span className="font-mono text-sm text-slate-700 dark:text-slate-200 shrink-0">{formatQuantity(item.quantity, item.unit)}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </div>
  );
}

export function TripsView({ reader, tripId, onSelect, nonce, canEdit, TripControls, onOpenWeek, onChanged }) {
  const [state, setState] = useState({ key: null });
  const [retry, setRetry] = useState(0);
  const requestKey = `${reader.mode}:${nonce}:${retry}`;
  useEffect(() => {
    let live = true;
    reader.listShoppingTrips()
      .then(result => { if (live) setState({ key: requestKey, trips: result.items }); })
      .catch(err => { if (live) setState({ key: requestKey, error: err?.message || 'Could not load shopping lists.' }); });
    return () => { live = false; };
  }, [reader, requestKey]);

  const current = state.key === requestKey ? state : (state.trips ? { ...state, stale: true } : null);
  const staticMode = reader.mode === 'static';
  const replaceTrip = trip => setState(previous => ({ ...previous, trips: previous.trips.map(item => item.trip_id === trip.trip_id ? trip : item) }));
  const removeTrip = id => { setState(previous => ({ ...previous, trips: previous.trips.filter(item => item.trip_id !== id) })); onSelect(null); onChanged?.(); };

  if (!current) return <div className="rounded-xl border border-dashed border-slate-300 dark:border-slate-700 p-10 text-center text-slate-600 dark:text-slate-300">Loading shopping lists…</div>;
  if (current.error) return <div role="alert" className="rounded-xl border border-dashed border-red-300 dark:border-red-900 p-8 text-center text-slate-700 dark:text-slate-300">Unable to load shopping lists: {current.error} <button className="ml-2 underline" onClick={() => setRetry(v => v + 1)}>Retry</button></div>;

  const trip = tripId ? current.trips.find(item => item.trip_id === tripId) : null;
  if (tripId && !trip) {
    return (
      <div className="space-y-4">
        <div role="alert" className="rounded-xl border border-dashed border-slate-300 dark:border-slate-700 p-8 text-center text-slate-600 dark:text-slate-300">{staticMode ? 'This shopping list is not in the published snapshot.' : 'This shopping list no longer exists.'}</div>
        <button type="button" onClick={() => onSelect(null)} className="min-h-11 px-4 rounded-lg bg-slate-100 dark:bg-slate-800 text-sm">Back to shopping lists</button>
      </div>
    );
  }
  if (!trip) {
    return (
      <div className="space-y-4" data-testid="trips-view">
        <div className="flex items-center gap-2"><ShoppingCart className="text-indigo-600 dark:text-indigo-400" /><h2 className="text-xl font-bold text-slate-800 dark:text-white">Shopping lists</h2></div>
        {staticMode && reader.context?.exported_at && <p className="text-xs text-slate-500 dark:text-slate-400">Snapshot as of {formatDateTime(reader.context.exported_at)}</p>}
        <TripList trips={current.trips} onOpen={onSelect} staticMode={staticMode} canEdit={canEdit} />
      </div>
    );
  }
  const controls = canEdit && TripControls ? (
    <Suspense fallback={null}>
      <TripControls key={trip.trip_id} trip={trip} reader={reader} onTrip={next => { replaceTrip(next); onChanged?.(); }} onDeleted={() => removeTrip(trip.trip_id)} />
    </Suspense>
  ) : null;
  return <div data-testid="trip-detail"><TripDetail key={`${trip.trip_id}:${trip.version}`} trip={trip} mode={reader.mode} exportedAt={reader.context?.exported_at} onBack={() => onSelect(null)} onOpenWeek={onOpenWeek} controls={controls} /></div>;
}

