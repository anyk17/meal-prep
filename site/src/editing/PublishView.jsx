import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPublishApi } from './publishApi.js';
import { formatMonth, formatWeekRange, monthOf } from '../dates.js';
import { MARKER_LABEL, POLL_INTERVAL_MS, POLL_TIMEOUT_MS, canPublishNow, defaultMonths, isBusy, jobStatusText, lastPublishedText, monthChoices, pollDecision, previewSummary, publishErrorMessage, toggleMonth, weekMarker } from '../publishModel.js';

const api = createPublishApi(import.meta.env.VITE_API_BASE_PATH || '/api/v1');
const btnPrimary = 'min-h-11 px-5 rounded-lg text-sm font-medium bg-indigo-600 text-white hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed';
const markerStyle = {
  new: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-200',
  changed: 'bg-amber-100 text-amber-900 dark:bg-amber-950/50 dark:text-amber-200',
  unchanged: 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300',
};
const formatWhen = value => { const date = new Date(value); return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date); };
const linkClass = 'inline-flex items-center min-h-11 underline text-indigo-700 dark:text-indigo-300';

// Publish view (home only): choose months, review what goes public, publish, then follow the job until the site is live.
export default function PublishView({ today }) {
  const currentMonth = monthOf(today);
  const [status, setStatus] = useState(null);
  const [selected, setSelected] = useState(() => defaultMonths(currentMonth));
  const [preview, setPreview] = useState(null);
  const [previewing, setPreviewing] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [job, setJob] = useState(null);
  const [message, setMessage] = useState('');
  const [timedOut, setTimedOut] = useState(false);
  const pollStart = useRef(0);
  const choices = useMemo(() => monthChoices(currentMonth), [currentMonth]);

  const loadStatus = useCallback(async () => {
    try { const next = await api.getStatus(); setStatus(next); return next; }
    catch (error) { setMessage(publishErrorMessage(error)); return null; }
  }, []);
  useEffect(() => {
    let live = true;
    api.getStatus().then(next => { if (!live) return; setStatus(next); if (isBusy(next.latest_job)) { setJob(next.latest_job); pollStart.current = Date.now(); } }).catch(error => { if (live) setMessage(publishErrorMessage(error)); });
    return () => { live = false; };
  }, []);

  const loadPreview = useCallback(async months => {
    setPreviewing(true); setPreview(null);
    try { const next = await api.preview(months); setPreview(next); setMessage(''); }
    catch (error) { setMessage(publishErrorMessage(error)); }
    finally { setPreviewing(false); }
  }, []);
  useEffect(() => {
    if (!selected.length) { setPreview(null); return undefined; }
    let live = true;
    setPreviewing(true); setPreview(null);
    api.preview(selected).then(next => { if (live) { setPreview(next); setMessage(''); } }).catch(error => { if (live) setMessage(publishErrorMessage(error)); }).finally(() => { if (live) setPreviewing(false); });
    return () => { live = false; };
  }, [selected]);

  // Poll the job every 10 seconds until it is live or failed, or ten minutes pass.
  const polling = isBusy(job);
  const jobId = job?.job_id;
  useEffect(() => {
    if (!polling) return undefined;
    let live = true, timer;
    const tick = async () => {
      const decision = pollDecision(job, Date.now() - pollStart.current, POLL_TIMEOUT_MS);
      if (decision === 'timeout') { setTimedOut(true); return; }
      try {
        const next = await api.getJob(jobId);
        if (!live) return;
        setJob(next);
        if (next.status === 'live' || next.status === 'failed') { void loadStatus(); void loadPreview(selected); return; }
      } catch (error) { if (live) setMessage(publishErrorMessage(error)); }
      if (live) timer = setTimeout(tick, POLL_INTERVAL_MS);
    };
    timer = setTimeout(tick, POLL_INTERVAL_MS);
    return () => { live = false; clearTimeout(timer); };
    // job is intentionally read once per job id; the effect restarts when the job id or busy state changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [polling, jobId, loadStatus, loadPreview]);

  const publish = async () => {
    setPublishing(true); setMessage(''); setTimedOut(false);
    try {
      const next = await api.publish(selected);
      pollStart.current = Date.now();
      setJob(next);
      if (next.status === 'unchanged') void loadStatus();
    } catch (error) { setMessage(publishErrorMessage(error)); }
    finally { setPublishing(false); }
  };

  if (status && !status.enabled) {
    return <section className="rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 p-4 text-sm" data-testid="publish-view">Publishing is not set up on this server.</section>;
  }
  const summary = previewSummary(preview);
  const ready = canPublishNow({ selected, preview, previewing, publishing, job });
  const siteUrl = status?.site_url;

  return (
    <section className="space-y-5 max-w-3xl" data-testid="publish-view" aria-label="Publish">
      <div>
        <h2 className="text-xl font-bold text-slate-800 dark:text-white">Publish to the public site</h2>
        <p className="text-sm text-slate-600 dark:text-slate-300 mt-1">{status ? lastPublishedText(status, formatWhen) : 'Loading…'}</p>
      </div>

      <div role="note" className="rounded-lg border border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/30 text-amber-950 dark:text-amber-100 px-4 py-3 text-sm">
        <strong>This is public.</strong> Everything published is readable by anyone with the link and stays in the public repository history, even after a later publication replaces it.
      </div>

      {message && <div role="alert" className="rounded-lg border border-red-200 dark:border-red-900/50 bg-red-50 dark:bg-red-950/30 text-red-800 dark:text-red-200 px-3 py-2 text-sm flex gap-3 justify-between"><span>{message}</span><button className="min-h-11 px-2 underline shrink-0" onClick={() => setMessage('')}>Dismiss</button></div>}

      <fieldset className="space-y-2">
        <legend className="text-xs font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400 mb-2">Months to publish</legend>
        <div className="flex flex-wrap gap-2">
          {choices.map(month => {
            const on = selected.includes(month);
            return (
              <label key={month} className={`inline-flex items-center gap-2 min-h-11 px-3 rounded-lg border text-sm cursor-pointer ${on ? 'bg-indigo-600 text-white border-indigo-600' : 'bg-white dark:bg-slate-900 border-slate-200 dark:border-slate-800'}`}>
                <input type="checkbox" className="w-5 h-5" checked={on} disabled={isBusy(job)} onChange={() => setSelected(previous => toggleMonth(previous, month))} />
                {formatMonth(month)}
              </label>
            );
          })}
        </div>
      </fieldset>

      <div className="rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 p-4 space-y-3" data-testid="publish-preview" aria-live="polite">
        <h3 className="font-semibold text-slate-800 dark:text-white">What will be published</h3>
        {!selected.length && <p className="text-sm text-slate-600 dark:text-slate-300">Choose at least one month.</p>}
        {previewing && <p className="text-sm text-slate-500">Loading preview…</p>}
        {preview && (
          <>
            {preview.unchanged && <p role="status" className="text-sm rounded-lg bg-slate-100 dark:bg-slate-800 px-3 py-2">Nothing has changed since the last publication for these months.</p>}
            {preview.weeks.length === 0
              ? <p className="text-sm text-slate-600 dark:text-slate-300">No saved weeks in the chosen months.</p>
              : <>
                  <p className="text-sm text-slate-600 dark:text-slate-300">{summary.weeks} {summary.weeks === 1 ? 'week' : 'weeks'} ({summary.new} new, {summary.changed} changed, {summary.unchanged} unchanged), with their groceries{summary.trips ? `, and ${summary.trips} ${summary.trips === 1 ? 'shopping list' : 'shopping lists'}` : ''}.</p>
                  <ul className="divide-y divide-slate-100 dark:divide-slate-800">
                    {preview.weeks.map(week => {
                      const marker = weekMarker(week);
                      return (
                        <li key={week.plan_id} className="py-2 flex flex-wrap items-center justify-between gap-2 text-sm">
                          <span>{formatWeekRange(week.week_start)} <span className="text-slate-500 dark:text-slate-400">· r{week.revision} · groceries included</span></span>
                          <span className={`px-2 py-1 rounded-md text-xs font-medium ${markerStyle[marker]}`}>{MARKER_LABEL[marker]}</span>
                        </li>
                      );
                    })}
                  </ul>
                </>}
            {preview.trips.length > 0 && (
              <div>
                <h4 className="text-xs font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400 mb-1">Shopping lists</h4>
                <ul className="text-sm space-y-1">{preview.trips.map(trip => <li key={trip.trip_id}>{trip.name} <span className="text-slate-500 dark:text-slate-400">({trip.status})</span></li>)}</ul>
              </div>
            )}
          </>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <button className={`${btnPrimary} w-full sm:w-auto`} disabled={!ready} onClick={publish}>{publishing ? 'Publishing…' : 'Publish'}</button>
      </div>

      {job && (
        <div role="status" className="rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 p-4 space-y-2 text-sm" data-testid="publish-job">
          <p className="font-medium">{jobStatusText(job)}</p>
          {timedOut && <p className="text-amber-800 dark:text-amber-200">Still waiting after 10 minutes. Check the GitHub Actions run, then reload this page.</p>}
          <div className="flex flex-wrap gap-x-4">
            {job.status === 'live' && siteUrl && <a className={linkClass} href={siteUrl} target="_blank" rel="noopener noreferrer">Open the public site</a>}
            {job.run_url && <a className={linkClass} href={job.run_url} target="_blank" rel="noopener noreferrer">GitHub Actions run</a>}
          </div>
        </div>
      )}
    </section>
  );
}
