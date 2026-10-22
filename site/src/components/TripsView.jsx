import { Suspense, useEffect, useState } from 'react';
import { ArrowLeft, Check, Plus, ShoppingCart } from 'lucide-react';
import { cn } from '../lib/utils';
import { formatWeekRange } from '../dates.js';
import { cellKey, cellOf, countChecked, formatQuantity, groupItemsByShelfLife, groupLabel, itemCheckKey, readTripChecks, readUnselected, selectedCells, selectionStorageKey, sortTrips, toBuyCells, toggleUnselected, totalsForCells, tripChanged, tripStorageKey, writeTripChecks, writeUnselected } from '../tripModel.js';

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
        <p className="text-sm text-slate-500 dark:text-slate-400">{staticMode ? 'Check back after the next publication.' : canEdit ? 'Tap New list, tick the weeks you want to shop for, and create it.' : 'Shopping lists can be created from the Month view at home.'}</p>
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
              <p className="text-sm text-slate-500 dark:text-slate-400">{rangeLabel(trip)} · {weeksLabel(trip)} · {trip.items.length} to buy{trip.bought_items?.length ? ` · ${trip.bought_items.length} bought` : ''}</p>
              {tripChanged(trip) && <p className="text-xs text-amber-700 dark:text-amber-300 mt-0.5">A plan changed since shopping</p>}
            </div>
            <StatusChip status={trip.status} />
          </button>
        </li>
      ))}
    </ul>
  );
}

const GROUPS = ['long_lasting', 'fresh'];

// Weeks x Long-lasting/Fresh. In editing mode a bought cell is a button that asks to undo.
function CoverageGrid({ trip, onOpenWeek, onUndo, undoKey }) {
  return (
    <table className="w-full text-sm border-separate border-spacing-y-1" aria-label="Weeks covered by this list" data-testid="coverage-grid">
      <thead>
        <tr className="text-xs text-slate-500 dark:text-slate-400 text-left">
          <th scope="col" className="font-medium pr-2">Week</th>
          {GROUPS.map(group => <th key={group} scope="col" className="font-medium px-1">{groupLabel(group)}</th>)}
        </tr>
      </thead>
      <tbody>
        {trip.weeks.map(week => (
          <tr key={week.plan_id} data-testid="grid-week">
            <th scope="row" className="font-normal text-left pr-2 align-middle">
              <button type="button" className="min-h-11 text-left hover:underline text-slate-800 dark:text-slate-100" onClick={() => onOpenWeek(week.week_start)}>{formatWeekRange(week.week_start)}</button>
            </th>
            {GROUPS.map(group => {
              const cell = cellOf(week, group);
              if (!cell) return <td key={group} className="px-1 text-slate-400 dark:text-slate-600 align-middle">—</td>;
              const bought = cell.state === 'bought';
              const label = `${groupLabel(group)}, ${formatWeekRange(week.week_start)}: ${bought ? 'bought' : 'to buy'}${cell.changed_since_shopping ? ', changed since shopping' : ''}`;
              const tone = bought ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300' : 'bg-indigo-100 text-indigo-800 dark:bg-indigo-900/40 dark:text-indigo-200';
              const body = (
                <>
                  <span className="font-medium">{bought ? '✓ Bought' : 'To buy'}</span>
                  {cell.changed_since_shopping && <span className="block text-[11px] leading-tight text-amber-700 dark:text-amber-300" data-testid="cell-changed">changed since shopping</span>}
                </>
              );
              return (
                <td key={group} className="px-1 align-middle" data-state={cell.state}>
                  {bought && onUndo
                    ? <button type="button" aria-label={`${label}. Undo`} aria-pressed={undoKey === cellKey(week.plan_id, group)} onClick={() => onUndo({ ...cell, plan_id: week.plan_id, week_start: week.week_start, key: cellKey(week.plan_id, group) })}
                        className={cn('w-full min-h-11 px-2 rounded-lg text-center border border-transparent hover:border-emerald-400', tone)}>{body}</button>
                    : <span className={cn('flex flex-col justify-center w-full min-h-11 px-2 rounded-lg text-center', tone)}>{body}</span>}
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ItemGroups({ groups, checks, onToggle }) {
  return groups.map(shelfGroup => (
    <div key={shelfGroup.group} role="group" aria-label={shelfGroup.label} data-group={shelfGroup.group} className="space-y-3">
      <h3 className="flex items-baseline justify-between gap-2 px-1 pt-2 text-lg font-bold text-slate-900 dark:text-white"><span>{shelfGroup.label}</span>{onToggle && <span className="text-sm font-mono font-semibold text-slate-500 dark:text-slate-400">{shelfGroup.checked}/{shelfGroup.total}</span>}</h3>
      {shelfGroup.sections.map(group => (
        <section key={group.section} aria-label={`${shelfGroup.label}: ${group.section}`} className={cn(card, 'overflow-hidden')}>
          <h4 className="px-4 py-2 text-xs font-semibold uppercase tracking-wider bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-200 flex justify-between"><span>{group.section}</span>{onToggle && <span className="font-mono normal-case">{group.checked}/{group.total}</span>}</h4>
          <ul className="divide-y divide-slate-100 dark:divide-slate-800">
            {group.items.map(item => {
              const on = !!checks[itemCheckKey(item)];
              if (!onToggle) {
                return (
                  <li key={itemCheckKey(item)} className="min-h-11 px-4 py-2 flex items-center gap-4">
                    <span className="flex-1 min-w-0 text-base text-slate-700 dark:text-slate-200">{item.display}</span>
                    <span className="font-mono text-sm text-slate-600 dark:text-slate-300 shrink-0">{formatQuantity(item.quantity, item.unit)}</span>
                  </li>
                );
              }
              return (
                <li key={itemCheckKey(item)}>
                  <button type="button" role="checkbox" aria-checked={on} onClick={() => onToggle(item)}
                    className={cn('w-full min-h-14 px-4 py-3 flex items-center gap-4 text-left select-none transition-opacity', on && 'opacity-50 bg-slate-50 dark:bg-slate-900/60')}>
                    <span className={cn('w-7 h-7 rounded-md border-2 flex items-center justify-center shrink-0', on ? 'bg-green-500 border-green-500 text-white' : 'border-slate-300 dark:border-slate-600')}>{on && <Check size={18} strokeWidth={3} />}</span>
                    <span className="flex-1 min-w-0"><span className={cn('block text-base font-medium text-slate-900 dark:text-slate-100', on && 'line-through')}>{item.display}</span></span>
                    <span className="font-mono text-sm text-slate-700 dark:text-slate-200 shrink-0">{formatQuantity(item.quantity, item.unit)}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </div>
  ));
}

// The cell picker for "Shop now": every to-buy cell, ticked by default. Per device, like item checkmarks.
function CellPicker({ cells, unselected, onToggle, onAll, onNone }) {
  const off = new Set(unselected);
  return (
    <fieldset className="rounded-lg border border-slate-200 dark:border-slate-700 p-3 space-y-2" data-testid="cell-picker">
      <legend className="px-1 text-sm font-semibold text-slate-800 dark:text-slate-100">What are you buying now?</legend>
      <ul className="grid gap-1 sm:grid-cols-2">
        {cells.map(cell => {
          const on = !off.has(cell.key);
          return (
            <li key={cell.key}>
              <button type="button" role="checkbox" aria-checked={on} onClick={() => onToggle(cell.key)}
                className={cn('w-full min-h-11 px-3 rounded-lg border flex items-center gap-3 text-left text-sm', on ? 'border-indigo-500 bg-indigo-50 dark:bg-indigo-950/40' : 'border-slate-300 dark:border-slate-700')}>
                <span className={cn('w-6 h-6 rounded-md border-2 flex items-center justify-center shrink-0', on ? 'bg-indigo-600 border-indigo-600 text-white' : 'border-slate-300 dark:border-slate-600')}>{on && <Check size={16} strokeWidth={3} />}</span>
                <span>{formatWeekRange(cell.week_start)} · <span className="font-medium">{groupLabel(cell.group)}</span></span>
              </button>
            </li>
          );
        })}
      </ul>
      <div className="flex gap-2">
        <button type="button" onClick={onAll} className="min-h-11 px-3 rounded-lg text-sm bg-slate-100 dark:bg-slate-800">Tick all</button>
        <button type="button" onClick={onNone} className="min-h-11 px-3 rounded-lg text-sm bg-slate-100 dark:bg-slate-800">Untick all</button>
      </div>
    </fieldset>
  );
}

function TripDetail({ trip, mode, exportedAt, onBack, onOpenWeek, renderControls, editable }) {
  const storageKey = tripStorageKey(mode, trip);
  const cellsKey = selectionStorageKey(mode, trip);
  const [state, setState] = useState(() => ({ key: storageKey, values: readTripChecks(storageKey) }));
  const [unselected, setUnselected] = useState(() => readUnselected(cellsKey));
  const [shopOpen, setShopOpen] = useState(false);
  const [undoCell, setUndoCell] = useState(null);
  const checks = state.key === storageKey ? state.values : readTripChecks(storageKey);
  useEffect(() => { if (state.key === storageKey) writeTripChecks(storageKey, state.values); }, [state, storageKey]);
  const update = change => setState(previous => {
    const current = previous.key === storageKey ? previous.values : readTripChecks(storageKey);
    return { key: storageKey, values: change(current) };
  });
  const toggle = item => update(current => { const next = { ...current }; const key = itemCheckKey(item); if (next[key]) delete next[key]; else next[key] = true; return next; });
  const saveUnselected = next => { setUnselected(next); writeUnselected(cellsKey, next); };

  const pending = toBuyCells(trip);
  const chosen = selectedCells(trip, unselected);
  const shownItems = totalsForCells(trip.items, chosen.map(cell => cell.key));
  const checked = countChecked(shownItems, checks);
  const shelfGroups = groupItemsByShelfLife(shownItems, checks);
  const boughtGroups = groupItemsByShelfLife(trip.bought_items ?? []);
  const partial = pending.length > 0 && chosen.length < pending.length;
  // After a shop, start the next one from everything still to buy (the unticked cells were the ones left for later).
  const onMarked = () => saveUnselected([]);
  const controls = renderControls?.({ selection: shopOpen ? chosen : null, onMarked, undoCell, onUndoClose: () => setUndoCell(null) });

  return (
    <div className="space-y-4">
      <button type="button" onClick={onBack} className="inline-flex items-center gap-1 min-h-11 text-sm text-indigo-700 dark:text-indigo-300"><ArrowLeft size={16} />All shopping lists</button>
      <header className={cn(card, 'p-4 space-y-3')}>
        <div className="flex items-start gap-3">
          <h2 className="text-xl font-bold text-slate-900 dark:text-white flex-1 min-w-0 break-words">{trip.name}</h2>
          <StatusChip status={trip.status} />
        </div>
        {mode === 'static' && exportedAt && <p className="text-xs text-slate-500 dark:text-slate-400">Snapshot as of {formatDateTime(exportedAt)}</p>}
        {trip.status === 'done' && trip.completed_at && <p className="text-xs text-slate-500 dark:text-slate-400">Everything bought {formatDateTime(trip.completed_at)}</p>}
        <CoverageGrid trip={trip} onOpenWeek={onOpenWeek} onUndo={editable ? setUndoCell : undefined} undoKey={undoCell?.key} />
        {pending.length > 0 && (
          <div className="space-y-2">
            <button type="button" aria-expanded={shopOpen} onClick={() => setShopOpen(open => !open)} className="inline-flex items-center gap-2 min-h-11 px-4 rounded-lg text-sm font-medium bg-indigo-600 text-white hover:bg-indigo-500">
              <ShoppingCart size={16} />{shopOpen ? 'Hide shop now' : 'Shop now'}
            </button>
            {shopOpen && <CellPicker cells={pending} unselected={unselected} onToggle={key => saveUnselected(toggleUnselected(unselected, key))} onAll={() => saveUnselected([])} onNone={() => saveUnselected(pending.map(cell => cell.key))} />}
          </div>
        )}
        {controls}
      </header>

      {pending.length === 0 ? (
        <p role="status" className="rounded-xl border border-dashed border-slate-300 dark:border-slate-700 p-6 text-center text-slate-600 dark:text-slate-300" data-testid="nothing-to-buy">Nothing left to buy. Every week on this list is bought.</p>
      ) : (
        <>
          <div className="flex items-center justify-between sticky top-16 z-20 bg-slate-50/95 dark:bg-slate-950/95 backdrop-blur py-2" data-testid="trip-progress">
            <p className="text-sm text-slate-600 dark:text-slate-300">To buy. Checkmarks stay on this device.</p>
            <span className="text-sm font-semibold px-3 py-1 rounded-full bg-slate-900 text-white dark:bg-slate-700" aria-live="polite">{checked} of {shownItems.length}</span>
          </div>
          {partial && <p className="text-sm text-slate-600 dark:text-slate-300" data-testid="partial-note">Totals for {chosen.length} of {pending.length} to-buy {pending.length === 1 ? 'cell' : 'cells'}. Open Shop now to change that.</p>}
          {chosen.length === 0 && <p role="status" className="text-sm text-slate-600 dark:text-slate-300">No cells are ticked. Open Shop now and tick what you are buying.</p>}
          <ItemGroups groups={shelfGroups} checks={checks} onToggle={toggle} />
        </>
      )}

      {boughtGroups.length > 0 && (
        <details className={cn(card, 'p-4')} data-testid="already-bought">
          <summary className="cursor-pointer min-h-11 flex items-center text-base font-semibold text-slate-800 dark:text-slate-100">Already bought ({trip.bought_items.length})</summary>
          <div className="space-y-3 pt-2"><ItemGroups groups={boughtGroups} checks={{}} /></div>
        </details>
      )}
    </div>
  );
}

export function TripsView({ reader, tripId, onSelect, nonce, canEdit, TripControls, onOpenWeek, onChanged, onNewList }) {
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
        <div className="flex items-center gap-2">
          <ShoppingCart className="text-indigo-600 dark:text-indigo-400" /><h2 className="text-xl font-bold text-slate-800 dark:text-white flex-1">Shopping lists</h2>
          {canEdit && onNewList && <button type="button" onClick={onNewList} className="inline-flex items-center gap-1 min-h-11 px-4 rounded-lg bg-indigo-600 text-white text-sm font-medium hover:bg-indigo-500"><Plus size={16} />New list</button>}
        </div>
        {staticMode && reader.context?.exported_at && <p className="text-xs text-slate-500 dark:text-slate-400">Snapshot as of {formatDateTime(reader.context.exported_at)}</p>}
        <TripList trips={current.trips} onOpen={onSelect} staticMode={staticMode} canEdit={canEdit} />
      </div>
    );
  }
  const editable = !!(canEdit && TripControls);
  const renderControls = extra => (editable ? (
    <Suspense fallback={null}>
      <TripControls key={trip.trip_id} trip={trip} reader={reader} {...extra} onTrip={next => { replaceTrip(next); onChanged?.(); }} onDeleted={() => removeTrip(trip.trip_id)} />
    </Suspense>
  ) : null);
  return <div data-testid="trip-detail"><TripDetail key={`${trip.trip_id}:${trip.version}`} trip={trip} mode={reader.mode} exportedAt={reader.context?.exported_at} onBack={() => onSelect(null)} onOpenWeek={onOpenWeek} renderControls={renderControls} editable={editable} /></div>;
}
