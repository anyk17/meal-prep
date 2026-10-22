import { useEffect, useState } from 'react';
import { createPlanEditor } from './planEditor.js';
import { runTripAction } from './tripActions.js';
import { addDays } from '../readers/shared.js';
import { formatWeekRange } from '../dates.js';
import { WHAT_TO_BUY, groupLabel, groupsForChoice, weekAvailability, weekGroupsLabel } from '../tripModel.js';

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

// Segmented "What to buy" control: Everything (default), Long-lasting only, Fresh only.
function WhatToBuy({ value, onChange, disabled }) {
  return (
    <div role="radiogroup" aria-label="What to buy" className="inline-flex rounded-lg border border-slate-200 dark:border-slate-800 overflow-hidden max-w-full">
      {WHAT_TO_BUY.map(option => (
        <button key={option.value} type="button" role="radio" aria-checked={value === option.value} disabled={disabled} onClick={() => onChange(option.value)}
          className={`min-h-11 px-2.5 text-xs sm:text-sm font-medium disabled:opacity-50 ${value === option.value ? 'bg-indigo-600 text-white' : 'bg-white dark:bg-slate-950 text-slate-700 dark:text-slate-200'}`}>{option.label}</button>
      ))}
    </div>
  );
}

// Edit controls for one trip: done/reopen/delete for any trip, add/remove weeks for an open one.
export default function TripControls({ trip, reader, onTrip, onDeleted }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [candidates, setCandidates] = useState([]);
  const [addPlan, setAddPlan] = useState('');
  const [choice, setChoice] = useState('both');
  const open = trip.status === 'open';

  useEffect(() => {
    if (!open) return undefined;
    let live = true;
    const from = addDays(trip.weeks[0].week_start, -56), to = addDays(trip.weeks.at(-1).week_end, 112);
    reader.getCoverage({ from, to }).then(result => { if (live) setCandidates(result.items); }).catch(() => { if (live) setCandidates([]); });
    return () => { live = false; };
  }, [open, reader, trip]);

  const chosenGroups = groupsForChoice(choice);
  const addable = candidates.filter(item => weekAvailability(item, chosenGroups, trip.trip_id).ok);
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
            {trip.weeks.map(week => {
              const lastWeek = trip.weeks.length < 2, both = week.groups.length > 1;
              const lastTitle = 'A list needs at least one week. Delete it instead.';
              return (
                <li key={week.plan_id} className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1 text-sm" data-testid="trip-week-edit">
                  <span>{formatWeekRange(week.week_start)} <span className="text-xs text-slate-500 dark:text-slate-400">· {weekGroupsLabel(week.groups)}</span></span>
                  <span className="flex flex-wrap gap-1">
                    {both && week.groups.map(group => (
                      <button key={group} className={btnQuiet} disabled={busy} onClick={() => act(() => getEditor().removeTripWeek(trip.trip_id, week.plan_id, trip.version, group))}>Remove {groupLabel(group)}</button>
                    ))}
                    <button className={btnQuiet} disabled={busy || lastWeek} title={lastWeek ? lastTitle : undefined} onClick={() => act(() => getEditor().removeTripWeek(trip.trip_id, week.plan_id, trip.version))}>Remove week</button>
                  </span>
                </li>
              );
            })}
          </ul>
          <WhatToBuy value={choice} onChange={value => { setChoice(value); setAddPlan(''); }} disabled={busy} />
          <div className="flex gap-2">
            <select aria-label="Week to add" value={addPlan} onChange={event => setAddPlan(event.target.value)} className="min-h-11 flex-1 min-w-0 px-3 rounded-lg bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 text-sm">
              <option value="">{addable.length ? 'Add a week…' : 'No available weeks nearby'}</option>
              {addable.map(item => <option key={item.plan_id} value={item.plan_id}>{formatWeekRange(item.week_start)}</option>)}
            </select>
            <button className={btnQuiet} disabled={busy || !addPlan} onClick={() => { const id = addPlan; setAddPlan(''); void act(() => getEditor().addTripWeek(trip.trip_id, id, trip.version, chosenGroups)); }}>Add week</button>
          </div>
        </div>
      )}
    </div>
  );
}

// Bottom bar shown in the month view while weeks are ticked: start a new list or add the weeks to an open one.
export function CreateTripBar({ planIds, coverage, reader, onClear, onCreated, onProblem }) {
  const [name, setName] = useState('');
  const [target, setTarget] = useState('');
  const [openTrips, setOpenTrips] = useState([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [choice, setChoice] = useState('both');
  const active = planIds.length > 0;

  useEffect(() => {
    if (!active) return undefined;
    let live = true;
    reader.listShoppingTrips().then(result => { if (live) setOpenTrips(result.items.filter(trip => trip.status === 'open')); }).catch(() => { if (live) setOpenTrips([]); });
    return () => { live = false; };
  }, [active, reader]);

  if (!active) return null;
  const chosen = openTrips.find(trip => trip.trip_id === target);
  const groups = groupsForChoice(choice);
  // Weeks whose chosen groups are already on another list cannot be added; say so instead of failing at the server.
  const problems = planIds.map(planId => ({ planId, week: coverage?.get(planId), availability: coverage?.get(planId) ? weekAvailability(coverage.get(planId), groups, chosen?.trip_id ?? null) : null }))
    .filter(entry => entry.availability && !entry.availability.ok);
  const finish = outcome => {
    setMessage(outcome.message);
    if (outcome.code === 'week_already_on_trip' || outcome.code === 'plan_not_found') onProblem?.();
  };
  const create = async () => {
    setBusy(true); setMessage('');
    const outcome = await runTripAction(() => getEditor().createTrip(planIds, name, groups), { reader });
    setBusy(false);
    if (outcome.trip) { setName(''); onCreated(outcome.trip); return; }
    finish(outcome);
  };
  // Adds the weeks one at a time, carrying the version each add returns; stops at the first problem.
  const addToTrip = async () => {
    setBusy(true); setMessage('');
    let trip = chosen;
    for (const planId of planIds) {
      const outcome = await runTripAction(() => getEditor().addTripWeek(trip.trip_id, planId, trip.version, groups), { reader, tripId: trip.trip_id });
      if (!outcome.trip || outcome.message) { setBusy(false); finish(outcome); return; }
      trip = outcome.trip;
    }
    setBusy(false); setTarget(''); onCreated(trip);
  };
  return (
    <div className="fixed bottom-0 inset-x-0 z-40 bg-white dark:bg-slate-900 border-t border-slate-200 dark:border-slate-800 shadow-lg p-3" data-testid="create-trip-bar">
      <div className="max-w-7xl mx-auto space-y-2">
        <Message message={message} onDismiss={() => setMessage('')} />
        <div className="flex flex-wrap items-center gap-2">
          <WhatToBuy value={choice} onChange={setChoice} disabled={busy} />
        </div>
        {problems.length > 0 && (
          <ul className="text-xs text-amber-800 dark:text-amber-200 space-y-0.5" data-testid="trip-bar-problems">
            {problems.map(({ planId, week, availability }) => <li key={planId}>Week of {formatWeekRange(week.week_start)}: {availability.reason || 'nothing left to add'}. Untick it or change what to buy.</li>)}
          </ul>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium">{planIds.length} {planIds.length === 1 ? 'week' : 'weeks'} selected</span>
          <select aria-label="Shopping list for the selected weeks" value={target} onChange={event => setTarget(event.target.value)} className="min-h-11 min-w-0 px-3 rounded-lg bg-white dark:bg-slate-950 border border-slate-200 dark:border-slate-800 text-sm">
            <option value="">New shopping list</option>
            {openTrips.map(trip => <option key={trip.trip_id} value={trip.trip_id}>Add to: {trip.name}</option>)}
          </select>
          {chosen
            ? <button className={btnPrimary} disabled={busy || problems.length > 0} onClick={addToTrip}>Add to list</button>
            : <>
                <input aria-label="Shopping list name" value={name} maxLength={80} onChange={event => setName(event.target.value)} placeholder="Name (optional)" className="min-h-11 flex-1 min-w-[10rem] px-3 rounded-lg bg-white dark:bg-slate-950 border border-slate-200 dark:border-slate-800 text-sm" />
                <button className={btnPrimary} disabled={busy || problems.length > 0} onClick={create}>Create shopping list</button>
              </>}
          <button className={btnQuiet} disabled={busy} onClick={onClear}>Clear</button>
        </div>
      </div>
    </div>
  );
}
