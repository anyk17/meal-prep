import { useEffect, useState } from 'react';
import { createPlanEditor } from './planEditor.js';
import { runTripAction } from './tripActions.js';
import { addDays } from '../readers/shared.js';
import { formatWeekRange } from '../dates.js';
import { SHELF_LIVES, addPlan, addableWeeks, addedMessage, chipRemoval, chipState, groupLabel, weekGroupsLabel, weekRemoval } from '../tripModel.js';

const API_BASE = import.meta.env.VITE_API_BASE_PATH || '/api/v1';
const btn = 'min-h-11 px-4 rounded-lg text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed';
const btnPrimary = `${btn} bg-indigo-600 text-white hover:bg-indigo-500`;
const btnQuiet = `${btn} bg-slate-100 dark:bg-slate-800 text-slate-800 dark:text-slate-100 hover:bg-slate-200 dark:hover:bg-slate-700`;
const btnDanger = `${btn} bg-red-50 dark:bg-red-950/30 text-red-700 dark:text-red-300 hover:bg-red-100 dark:hover:bg-red-950/50`;
let editor = null;
const getEditor = () => (editor ??= createPlanEditor(API_BASE));

function Message({ message, onDismiss, info = false }) {
  if (!message) return null;
  const tone = info ? 'border-sky-200 dark:border-sky-900/50 bg-sky-50 dark:bg-sky-950/30 text-sky-800 dark:text-sky-200' : 'border-red-200 dark:border-red-900/50 bg-red-50 dark:bg-red-950/30 text-red-800 dark:text-red-200';
  return <div role={info ? 'status' : 'alert'} className={`rounded-lg border ${tone} px-3 py-2 text-sm flex gap-3 justify-between`}><span>{message}</span><button className="underline shrink-0" onClick={onDismiss}>Dismiss</button></div>;
}

const chipBase = 'min-h-11 px-3 rounded-lg text-sm font-medium border transition-colors disabled:cursor-not-allowed';
const chipOn = `${chipBase} bg-indigo-600 border-indigo-600 text-white hover:bg-indigo-500 disabled:opacity-50`;
const chipOff = `${chipBase} bg-white dark:bg-slate-950 border-slate-300 dark:border-slate-700 text-slate-700 dark:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800 disabled:opacity-50`;

// One group toggle on a week row. ON removes the group, OFF adds it, blocked shows which list has it.
function GroupChip({ group, chip, removal, busy, onToggle }) {
  const on = chip.state === 'on';
  const blocked = chip.state === 'off-blocked' || chip.state === 'off-unknown';
  const lastBlocked = on && removal.kind === 'blocked';
  const note = chip.state === 'off-blocked' ? (chip.otherList ? `on '${chip.otherList}'` : 'on another list') : '';
  return (
    <span className="inline-flex flex-col items-start">
      <button type="button" aria-pressed={on} className={on ? chipOn : chipOff} disabled={busy || blocked || lastBlocked} title={lastBlocked ? removal.hint : note || undefined} onClick={onToggle}>
        {on ? '✓ ' : ''}{groupLabel(group)}
      </button>
      {note && <span className="text-[11px] leading-tight text-slate-500 dark:text-slate-400 max-w-[9rem]">{note}</span>}
    </span>
  );
}

// Edit controls for one trip: done/reopen/delete for any trip, add/remove weeks for an open one.
export default function TripControls({ trip, reader, onTrip, onDeleted }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [candidates, setCandidates] = useState([]);
  const [addPlanId, setAddPlan] = useState('');
  const [tripNames, setTripNames] = useState(() => new Map());
  const [info, setInfo] = useState('');
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const open = trip.status === 'open';

  useEffect(() => {
    if (!open) return undefined;
    let live = true;
    const from = addDays(trip.weeks[0].week_start, -56), to = addDays(trip.weeks.at(-1).week_end, 112);
    reader.getCoverage({ from, to }).then(result => { if (live) setCandidates(result.items); }).catch(() => { if (live) setCandidates([]); });
    reader.listShoppingTrips().then(result => { if (live) setTripNames(new Map(result.items.map(item => [item.trip_id, item.name]))); }).catch(() => { if (live) setTripNames(new Map()); });
    return () => { live = false; };
  }, [open, reader, trip]);

  const addable = addableWeeks(candidates, trip);
  const coverageOf = planId => candidates.find(item => item.plan_id === planId);
  const act = async (action) => {
    setBusy(true); setMessage(''); setInfo('');
    const outcome = await runTripAction(action, { reader, tripId: trip.trip_id });
    setBusy(false);
    if (outcome.deleted || outcome.gone) { if (outcome.message) setMessage(outcome.message); onDeleted(); return; }
    if (outcome.message) setMessage(outcome.message);
    if (outcome.trip) onTrip(outcome.trip);
  };
  const addWeek = async () => {
    const item = candidates.find(entry => entry.plan_id === addPlanId);
    if (!item) return;
    const plan = addPlan(item, tripNames);
    setAddPlan('');
    if (!plan.groups.length) { setMessage('Nothing is free to add for that week.'); return; }
    setBusy(true); setMessage(''); setInfo('');
    const outcome = await runTripAction(() => getEditor().addTripWeek(trip.trip_id, item.plan_id, trip.version, plan.groups), { reader, tripId: trip.trip_id });
    setBusy(false);
    if (outcome.message) setMessage(outcome.message);
    else if (plan.skipped.length) setInfo(addedMessage(formatWeekRange(item.week_start), plan.groups, plan.skipped));
    if (outcome.trip) onTrip(outcome.trip);
  };
  const deleteTrip = () => { setConfirmingDelete(false); void act(() => getEditor().deleteTrip(trip.trip_id, trip.version)); };

  return (
    <div className="space-y-3 border-t border-slate-100 dark:border-slate-800 pt-3" data-testid="trip-controls">
      <Message message={message} onDismiss={() => setMessage('')} />
      <Message info message={info} onDismiss={() => setInfo('')} />
      <div className="flex flex-wrap gap-2">
        {open
          ? <button className={btnPrimary} disabled={busy} onClick={() => act(() => getEditor().markTripDone(trip.trip_id, trip.version))}>Shopping done</button>
          : <button className={btnPrimary} disabled={busy} onClick={() => act(() => getEditor().reopenTrip(trip.trip_id, trip.version))}>Reopen</button>}
        {!confirmingDelete && <button className={btnDanger} disabled={busy} onClick={() => setConfirmingDelete(true)}>Delete</button>}
      </div>
      {confirmingDelete && (
        <div role="alertdialog" aria-label="Confirm delete" className="rounded-lg border border-red-200 dark:border-red-900/50 bg-red-50 dark:bg-red-950/30 px-4 py-3 text-sm space-y-2 text-red-900 dark:text-red-100">
          <p>Delete the shopping list "{trip.name}"? Its weeks become uncovered. Meal plans are not changed.</p>
          <div className="flex gap-2">
            <button className={`${btn} bg-red-600 text-white hover:bg-red-500`} disabled={busy} onClick={deleteTrip}>Yes, delete</button>
            <button className={btnQuiet} disabled={busy} onClick={() => setConfirmingDelete(false)}>Cancel</button>
          </div>
        </div>
      )}
      {open && (
        <div className="space-y-2">
          <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">Edit weeks</h3>
          <ul className="space-y-2">
            {trip.weeks.map(week => {
              const coverage = coverageOf(week.plan_id);
              const weekGuard = weekRemoval(trip);
              const remove = group => act(() => getEditor().removeTripWeek(trip.trip_id, week.plan_id, trip.version, group));
              return (
                <li key={week.plan_id} className="space-y-1" data-testid="trip-week-edit">
                  <div className="flex items-center justify-between gap-2 text-sm">
                    <span className="font-medium">{formatWeekRange(week.week_start)}</span>
                    <button type="button" className={`${btnQuiet} px-3`} aria-label={`Remove week ${formatWeekRange(week.week_start)}`} title={weekGuard.kind === 'blocked' ? weekGuard.hint : 'Remove week'} disabled={busy || weekGuard.kind === 'blocked'} onClick={() => remove()}>✕</button>
                  </div>
                  <div className="flex flex-wrap items-start gap-2">
                    {SHELF_LIVES.map(group => {
                      const chip = chipState({ tripWeek: week, coverage, group, tripId: trip.trip_id, tripNames });
                      const removal = chip.state === 'on' ? chipRemoval(trip, week, group) : { kind: 'none' };
                      const toggle = () => {
                        if (chip.state === 'on') { if (removal.kind === 'remove_group') void remove(group); else if (removal.kind === 'remove_week') void remove(); }
                        else if (chip.state === 'off-free') void act(() => getEditor().addTripWeek(trip.trip_id, week.plan_id, trip.version, [group]));
                      };
                      return <GroupChip key={group} group={group} chip={chip} removal={removal} busy={busy} onToggle={toggle} />;
                    })}
                  </div>
                  {trip.weeks.length < 2 && <p className="text-xs text-slate-500 dark:text-slate-400">{weekGuard.hint}</p>}
                </li>
              );
            })}
          </ul>
          <div className="flex gap-2">
            <select aria-label="Week to add" value={addPlanId} onChange={event => setAddPlan(event.target.value)} className="min-h-11 flex-1 min-w-0 px-3 rounded-lg bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 text-sm">
              <option value="">{addable.length ? 'Add a week…' : 'No available weeks nearby'}</option>
              {addable.map(item => <option key={item.plan_id} value={item.plan_id}>{formatWeekRange(item.week_start)}</option>)}
            </select>
            <button className={btnQuiet} disabled={busy || !addPlanId} onClick={() => addWeek()}>Add week</button>
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
  const [allTrips, setAllTrips] = useState([]);
  const [info, setInfo] = useState('');
  const active = planIds.length > 0;

  useEffect(() => {
    if (!active) return undefined;
    let live = true;
    reader.listShoppingTrips().then(result => { if (live) setOpenTrips(result.items.filter(trip => trip.status === 'open')); setAllTrips(result.items); }).catch(() => { if (live) { setOpenTrips([]); setAllTrips([]); } });
    return () => { live = false; };
  }, [active, reader]);

  if (!active) return null;
  const chosen = openTrips.find(trip => trip.trip_id === target);
  const names = new Map(allTrips.map(trip => [trip.trip_id, trip.name]));
  // Every ticked week gets the groups that are free (on no list); groups on another list are skipped, not an error.
  const plans = planIds.map(planId => {
    const week = coverage?.get(planId);
    return { planId, week, ...(week ? addPlan(week, names) : { groups: [], skipped: [] }) };
  });
  const problems = plans.filter(entry => entry.week && !entry.groups.length);
  const skippedNotes = plans.filter(entry => entry.skipped.length);
  const skipText = entry => entry.skipped.map(skip => `${groupLabel(skip.group)} is ${skip.tripName ? `on '${skip.tripName}'` : 'on another list'}`).join(', ');
  const finish = outcome => {
    setMessage(outcome.message);
    if (outcome.code === 'week_already_on_trip' || outcome.code === 'plan_not_found') onProblem?.();
  };
  const skippedSummary = () => skippedNotes.map(entry => `${formatWeekRange(entry.week.week_start)}: ${skipText(entry)}`).join('; ');
  // Create takes one group set for all weeks, so start with the first week and add the rest with their own groups.
  const create = async () => {
    setBusy(true); setMessage(''); setInfo('');
    const [first, ...rest] = plans;
    let outcome = await runTripAction(() => getEditor().createTrip([first.planId], name, first.groups), { reader });
    if (!outcome.trip) { setBusy(false); finish(outcome); return; }
    let trip = outcome.trip;
    for (const entry of rest) {
      outcome = await runTripAction(() => getEditor().addTripWeek(trip.trip_id, entry.planId, trip.version, entry.groups), { reader, tripId: trip.trip_id });
      if (!outcome.trip || outcome.message) {
        setBusy(false); setName('');
        if (outcome.trip) onCreated(outcome.trip);
        setMessage(`The list was created but not every week could be added. ${outcome.message ?? ''}`.trim());
        if (outcome.code === 'week_already_on_trip' || outcome.code === 'plan_not_found') onProblem?.();
        return;
      }
      trip = outcome.trip;
    }
    setBusy(false); setName('');
    const summary = skippedSummary();
    onCreated(trip);
    if (summary) setInfo(`Some groups were left out: ${summary}`);
  };
  // Adds the weeks one at a time, carrying the version each add returns; stops at the first problem.
  const addToTrip = async () => {
    setBusy(true); setMessage(''); setInfo('');
    let trip = chosen;
    for (const entry of plans) {
      const outcome = await runTripAction(() => getEditor().addTripWeek(trip.trip_id, entry.planId, trip.version, entry.groups), { reader, tripId: trip.trip_id });
      if (!outcome.trip || outcome.message) { setBusy(false); finish(outcome); return; }
      trip = outcome.trip;
    }
    setBusy(false); setTarget('');
    const summary = skippedSummary();
    onCreated(trip);
    if (summary) setInfo(`Some groups were left out: ${summary}`);
  };
  return (
    <div className="fixed bottom-0 inset-x-0 z-40 bg-white dark:bg-slate-900 border-t border-slate-200 dark:border-slate-800 shadow-lg p-3" data-testid="create-trip-bar">
      <div className="max-w-7xl mx-auto space-y-2">
        <Message message={message} onDismiss={() => setMessage('')} />
        <Message info message={info} onDismiss={() => setInfo('')} />
        {problems.length > 0 && (
          <ul className="text-xs text-amber-800 dark:text-amber-200 space-y-0.5" data-testid="trip-bar-problems">
            {problems.map(({ planId, week }) => <li key={planId}>Week of {formatWeekRange(week.week_start)}: nothing left to add. Untick it.</li>)}
          </ul>
        )}
        {problems.length === 0 && skippedNotes.length > 0 && (
          <ul className="text-xs text-slate-600 dark:text-slate-300 space-y-0.5" data-testid="trip-bar-skipped">
            {skippedNotes.map(entry => <li key={entry.planId}>Week of {formatWeekRange(entry.week.week_start)}: {weekGroupsLabel(entry.groups)} will be added ({skipText(entry)}).</li>)}
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
