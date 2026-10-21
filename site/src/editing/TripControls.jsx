import { useEffect, useState } from 'react';
import { createPlanEditor } from './planEditor.js';
import { runTripAction } from './tripActions.js';
import { addDays } from '../readers/shared.js';
import { formatWeekRange } from '../dates.js';

const API_BASE = import.meta.env.VITE_API_BASE_PATH || '/api/v1';
const btn = 'min-h-11 px-4 rounded-lg text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed';
const btnPrimary = `${btn} bg-indigo-600 text-white hover:bg-indigo-500`;
const btnQuiet = `${btn} bg-slate-100 dark:bg-slate-800 text-slate-800 dark:text-slate-100 hover:bg-slate-200 dark:hover:bg-slate-700`;
const btnDanger = `${btn} bg-red-50 dark:bg-red-950/30 text-red-700 dark:text-red-300 hover:bg-red-100 dark:hover:bg-red-950/50`;
let editor = null;
const getEditor = () => (editor ??= createPlanEditor(API_BASE));

function Message({ message, onDismiss }) {
  return message ? <div role="alert" className="rounded-lg border border-red-200 dark:border-red-900/50 bg-red-50 dark:bg-red-950/30 text-red-800 dark:text-red-200 px-3 py-2 text-sm flex gap-3 justify-between"><span>{message}</span><button className="underline shrink-0" onClick={onDismiss}>Dismiss</button></div> : null;
}

// Edit controls for one trip: done/reopen/delete for any trip, add/remove weeks for an open one.
export default function TripControls({ trip, reader, onTrip, onDeleted }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [candidates, setCandidates] = useState([]);
  const [addPlan, setAddPlan] = useState('');
  const open = trip.status === 'open';

  useEffect(() => {
    if (!open) return undefined;
    let live = true;
    const from = addDays(trip.weeks[0].week_start, -56), to = addDays(trip.weeks.at(-1).week_end, 112);
    reader.getCoverage({ from, to }).then(result => { if (live) setCandidates(result.items.filter(item => item.coverage === 'not_covered')); }).catch(() => { if (live) setCandidates([]); });
    return () => { live = false; };
  }, [open, reader, trip]);

  const act = async (action) => {
    setBusy(true); setMessage('');
    const outcome = await runTripAction(action, { reader, tripId: trip.trip_id });
    setBusy(false);
    if (outcome.deleted || outcome.gone) { if (outcome.message) setMessage(outcome.message); onDeleted(); return; }
    if (outcome.message) setMessage(outcome.message);
    if (outcome.trip) onTrip(outcome.trip);
  };
  const confirmDelete = () => {
    if (window.confirm(`Delete the shopping list "${trip.name}"? Its weeks become uncovered. Plans are not changed.`)) void act(() => getEditor().deleteTrip(trip.trip_id, trip.version));
  };

  return (
    <div className="space-y-3 border-t border-slate-100 dark:border-slate-800 pt-3" data-testid="trip-controls">
      <Message message={message} onDismiss={() => setMessage('')} />
      <div className="flex flex-wrap gap-2">
        {open
          ? <button className={btnPrimary} disabled={busy} onClick={() => act(() => getEditor().markTripDone(trip.trip_id, trip.version))}>Shopping done</button>
          : <button className={btnPrimary} disabled={busy} onClick={() => act(() => getEditor().reopenTrip(trip.trip_id, trip.version))}>Reopen</button>}
        <button className={btnDanger} disabled={busy} onClick={confirmDelete}>Delete</button>
      </div>
      {open && (
        <div className="space-y-2">
          <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">Edit weeks</h3>
          <ul className="space-y-1">
            {trip.weeks.map(week => (
              <li key={week.plan_id} className="flex items-center justify-between gap-2 text-sm">
                <span>{formatWeekRange(week.week_start)}</span>
                <button className={btnQuiet} disabled={busy || trip.weeks.length < 2} title={trip.weeks.length < 2 ? 'A list needs at least one week. Delete it instead.' : undefined} onClick={() => act(() => getEditor().removeTripWeek(trip.trip_id, week.plan_id, trip.version))}>Remove week</button>
              </li>
            ))}
          </ul>
          <div className="flex gap-2">
            <select aria-label="Week to add" value={addPlan} onChange={event => setAddPlan(event.target.value)} className="min-h-11 flex-1 min-w-0 px-3 rounded-lg bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 text-sm">
              <option value="">{candidates.length ? 'Add a week…' : 'No uncovered weeks nearby'}</option>
              {candidates.map(item => <option key={item.plan_id} value={item.plan_id}>{formatWeekRange(item.week_start)}</option>)}
            </select>
            <button className={btnQuiet} disabled={busy || !addPlan} onClick={() => { const id = addPlan; setAddPlan(''); void act(() => getEditor().addTripWeek(trip.trip_id, id, trip.version)); }}>Add week</button>
          </div>
        </div>
      )}
    </div>
  );
}

// Bottom bar shown in the month view while weeks are ticked.
export function CreateTripBar({ planIds, reader, onClear, onCreated, onProblem }) {
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  if (!planIds.length) return null;
  const create = async () => {
    setBusy(true); setMessage('');
    const outcome = await runTripAction(() => getEditor().createTrip(planIds, name), { reader });
    setBusy(false);
    if (outcome.trip) { setName(''); onCreated(outcome.trip); return; }
    setMessage(outcome.message);
    if (outcome.code === 'week_already_on_trip' || outcome.code === 'plan_not_found') onProblem?.();
  };
  return (
    <div className="fixed bottom-0 inset-x-0 z-40 bg-white dark:bg-slate-900 border-t border-slate-200 dark:border-slate-800 shadow-lg p-3" data-testid="create-trip-bar">
      <div className="max-w-7xl mx-auto space-y-2">
        <Message message={message} onDismiss={() => setMessage('')} />
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium">{planIds.length} {planIds.length === 1 ? 'week' : 'weeks'} selected</span>
          <input aria-label="Shopping list name" value={name} maxLength={80} onChange={event => setName(event.target.value)} placeholder="Name (optional)" className="min-h-11 flex-1 min-w-[10rem] px-3 rounded-lg bg-white dark:bg-slate-950 border border-slate-200 dark:border-slate-800 text-sm" />
          <button className={btnPrimary} disabled={busy} onClick={create}>Create shopping list</button>
          <button className={btnQuiet} disabled={busy} onClick={onClear}>Clear</button>
        </div>
      </div>
    </div>
  );
}
