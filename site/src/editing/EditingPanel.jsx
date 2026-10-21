import { useCallback, useEffect, useMemo, useState } from 'react';
import { CalendarPlus, History, RefreshCw, Undo2, Check, X, CalendarRange } from 'lucide-react';
import { createPlanEditor } from './planEditor.js';
import { formatMonth, todayIn } from '../dates.js';

const API_BASE = import.meta.env.VITE_API_BASE_PATH || '/api/v1';
const btn = 'px-3 py-2 rounded-lg text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed';
const btnPrimary = `${btn} bg-indigo-600 text-white hover:bg-indigo-500`;
const btnQuiet = `${btn} bg-slate-100 dark:bg-slate-800 text-slate-800 dark:text-slate-100 hover:bg-slate-200 dark:hover:bg-slate-700`;
const STATUS_LABEL = { created: 'Created', exists: 'Already had a plan', skipped_past: 'Past week, skipped', failed: 'Failed' };
const STATUS_STYLE = { created: 'text-emerald-600 dark:text-emerald-400', exists: 'text-slate-500', skipped_past: 'text-slate-500', failed: 'text-red-600 dark:text-red-400' };

function formatTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date);
}
const slotLabel = change => `${change.date} ${change.meal}`;

function Problem({ problem, busy, cooldown, onRefresh, onRegenerate, onDismiss }) {
  const { code, message, details = [] } = problem;
  let body;
  let action = null;
  if (code === 'stale_revision' || code === 'input_basis_changed') {
    body = 'This plan changed since you loaded it (another edit, or recipes/rules/pantry were updated).';
    action = <button className={btnPrimary} disabled={busy} onClick={onRefresh}>Refresh plan</button>;
  } else if (code === 'proposal_expired') {
    body = 'This proposal expired before it was accepted.';
    action = onRegenerate && <button className={btnPrimary} disabled={busy} onClick={onRegenerate}>Regenerate</button>;
  } else if (code === 'generation_busy') {
    body = cooldown > 0 ? `The planner is busy. Try again in ${cooldown} s.` : 'The planner is busy. You can try again now.';
  } else if (code === 'week_not_editable') {
    body = 'Weeks that have started or passed are read-only. Only future weeks can be regenerated or reverted.';
  } else if (code === 'rule_violation') {
    body = 'The generated plan breaks weekly rules, so nothing was saved:';
  } else if (code === 'duplicate_week') {
    body = 'This week already has a plan.';
    action = <button className={btnPrimary} disabled={busy} onClick={onRefresh}>Refresh plan</button>;
  } else {
    body = message;
  }
  return (
    <div role="alert" className="rounded-lg border border-red-200 dark:border-red-900/50 bg-red-50 dark:bg-red-950/30 text-red-800 dark:text-red-200 px-4 py-3 text-sm space-y-2">
      <p>{body}</p>
      {code === 'rule_violation' && details.length > 0 && (
        <ul className="list-disc pl-5">{details.map((d, i) => <li key={i}><span className="font-mono">{d.field}</span>: {d.message}</li>)}</ul>
      )}
      {!['rule_violation', 'generation_busy'].includes(code) && body !== message && message && <p className="opacity-75">{message}</p>}
      <div className="flex gap-2">{action}<button className={btnQuiet} onClick={onDismiss}>Dismiss</button></div>
    </div>
  );
}

function ProposalReview({ proposal, busy, onAccept, onDiscard }) {
  const { changes, identical } = proposal;
  return (
    <section aria-label="Proposal review" className="rounded-xl border border-indigo-200 dark:border-indigo-900/60 bg-indigo-50/60 dark:bg-indigo-950/20 p-4 space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="font-bold text-slate-800 dark:text-white">Proposed plan (not saved yet)</h3>
          <p className="text-xs text-slate-500 dark:text-slate-400">Based on revision {proposal.base_revision}, seed {proposal.seed}. Expires {formatTime(proposal.expires_at)}.</p>
        </div>
        <div className="flex gap-2">
          <button className={btnPrimary} disabled={busy || identical} onClick={onAccept}><Check size={14} className="inline mr-1" />Accept</button>
          <button className={btnQuiet} disabled={busy} onClick={onDiscard}><X size={14} className="inline mr-1" />Discard</button>
        </div>
      </div>
      {identical && <p className="text-sm text-slate-700 dark:text-slate-300">This proposal is identical to the current plan, so there is nothing to accept. Discard it and regenerate for another variation.</p>}
      {!identical && (
        <ul className="text-sm space-y-1" aria-label="Changes">
          {changes.map(change => (
            <li key={`${change.assignment_id}-${change.kind}`} className="text-slate-700 dark:text-slate-300">
              <span className="font-mono text-xs uppercase mr-2">{slotLabel(change)}</span>
              <span className="line-through opacity-70">{change.before?.recipe_name ?? '(none)'}</span>
              <span className="mx-1">→</span>
              <span className="font-semibold">{change.after?.recipe_name ?? '(removed)'}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export default function EditingPanel({ weekStart, plan, review, onReview, onReload }) {
  const editor = useMemo(() => createPlanEditor(API_BASE), []);
  const [busy, setBusy] = useState(null);
  const [problem, setProblem] = useState(null);
  const [cooldown, setCooldown] = useState(0);
  const [confirmUndo, setConfirmUndo] = useState(false);
  const [fillResult, setFillResult] = useState(null);
  const [revisions, setRevisions] = useState(null);

  const today = todayIn(plan?.timezone);
  const weekEnd = new Date(Date.parse(`${weekStart}T00:00:00Z`) + 6 * 86400000).toISOString().slice(0, 10);
  const canCreate = !plan && weekEnd >= today;
  const canRegenerate = !!plan && plan.week_start > today;
  const canUndo = canRegenerate && plan.revision > 1;
  const month = weekStart.slice(0, 7);
  const isBusy = busy !== null;
  const planKey = plan ? `${plan.plan_id}:${plan.revision}:${plan.snapshot_id}` : null;

  useEffect(() => {
    if (cooldown <= 0) return undefined;
    const timer = setTimeout(() => setCooldown(value => Math.max(0, value - 1)), 1000);
    return () => clearTimeout(timer);
  }, [cooldown]);

  useEffect(() => {
    if (!plan) { setRevisions(null); return undefined; }
    let live = true;
    editor.listRevisions(plan.plan_id).then(result => { if (live) setRevisions(result.items); }).catch(() => { if (live) setRevisions(null); });
    return () => { live = false; };
    // Reload history whenever the displayed plan identity changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editor, planKey]);

  const run = useCallback(async (name, work) => {
    if (busy !== null) return;
    setBusy(name); setProblem(null);
    try { await work(); }
    catch (err) {
      setProblem({ code: err?.code || 'source_unavailable', message: err?.message || 'The request failed.', details: err?.details || [] });
      if (err?.code === 'generation_busy') setCooldown(err.retryAfter ?? 5);
    } finally { setBusy(null); }
  }, [busy]);

  const refresh = () => { onReview(null); setProblem(null); void onReload(true); };
  const create = () => run('create', async () => { await editor.createPlan(weekStart); await onReload(); });
  const propose = () => run('propose', async () => { onReview(await editor.propose(plan.plan_id, plan.revision)); });
  const accept = () => run('accept', async () => {
    await editor.acceptProposal(review.plan_id, review.proposal_id, review.base_revision);
    onReview(null);
    await onReload();
  });
  const discard = () => run('discard', async () => {
    const proposal = review;
    onReview(null);
    try { await editor.discardProposal(proposal.plan_id, proposal.proposal_id); }
    catch (err) { if (err?.code !== 'proposal_not_found' && err?.code !== 'proposal_expired') throw err; }
  });
  const undo = () => run('undo', async () => { setConfirmUndo(false); await editor.undo(plan.plan_id, plan.revision); await onReload(); });
  const fill = () => run('fill', async () => {
    setFillResult(null);
    setFillResult(await editor.fillMonth(month));
    await onReload();
  });

  const disableGenerate = isBusy || cooldown > 0;
  return (
    <div className="space-y-4" data-testid="editing-panel">
      <div className="flex flex-wrap items-center gap-2">
        {canCreate && <button className={btnPrimary} disabled={disableGenerate} onClick={create}><CalendarPlus size={14} className="inline mr-1" />{busy === 'create' ? 'Creating…' : 'Create plan'}</button>}
        {canRegenerate && !review && <button className={btnPrimary} disabled={disableGenerate} onClick={propose}><RefreshCw size={14} className="inline mr-1" />{busy === 'propose' ? 'Generating proposal…' : 'Regenerate'}</button>}
        {canUndo && !review && !confirmUndo && <button className={btnQuiet} disabled={isBusy} onClick={() => setConfirmUndo(true)}><Undo2 size={14} className="inline mr-1" />Undo last change</button>}
        <button className={btnQuiet} disabled={disableGenerate} onClick={fill}><CalendarRange size={14} className="inline mr-1" />{busy === 'fill' ? `Generating ${formatMonth(month)}…` : `Generate ${formatMonth(month)}`}</button>
        {plan && !canRegenerate && <span className="text-xs text-slate-500 dark:text-slate-400">This week has started, so it is read-only.</span>}
      </div>

      {busy === 'fill' && <p role="status" className="text-sm text-slate-600 dark:text-slate-300">Generating missing weeks one at a time. This can take a minute; keep this page open.</p>}

      {confirmUndo && plan && (
        <div role="alertdialog" aria-label="Confirm undo" className="rounded-lg border border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/30 px-4 py-3 text-sm space-y-2 text-amber-900 dark:text-amber-100">
          <p>Restore the version before revision {plan.revision}? This saves the earlier meals as a new revision, and groceries return to match them.</p>
          <div className="flex gap-2">
            <button className={btnPrimary} disabled={isBusy} onClick={undo}>{busy === 'undo' ? 'Undoing…' : 'Yes, undo'}</button>
            <button className={btnQuiet} disabled={isBusy} onClick={() => setConfirmUndo(false)}>Cancel</button>
          </div>
        </div>
      )}

      {problem && <Problem problem={problem} busy={isBusy} cooldown={cooldown} onRefresh={refresh} onRegenerate={plan && canRegenerate ? () => { onReview(null); void propose(); } : null} onDismiss={() => setProblem(null)} />}

      {review && <ProposalReview proposal={review} busy={isBusy} onAccept={accept} onDiscard={discard} />}

      {fillResult && (
        <section aria-label="Month result" className="rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 p-4 text-sm">
          <div className="flex items-center justify-between mb-2">
            <h3 className="font-bold text-slate-800 dark:text-white">{formatMonth(fillResult.month)} results</h3>
            <button className={btnQuiet} onClick={() => setFillResult(null)}>Dismiss</button>
          </div>
          <ul className="space-y-1">
            {fillResult.weeks.map(week => (
              <li key={week.week_start} className="flex flex-wrap gap-x-3">
                <span className="font-mono">{week.week_start} – {week.week_end}</span>
                <span className={STATUS_STYLE[week.status]}>{STATUS_LABEL[week.status] || week.status}</span>
                {week.status === 'failed' && week.error && <span className="text-slate-500 dark:text-slate-400">{week.error.message}</span>}
              </li>
            ))}
          </ul>
        </section>
      )}

      {revisions && revisions.length > 0 && (
        <details className="text-sm text-slate-600 dark:text-slate-300">
          <summary className="cursor-pointer inline-flex items-center gap-1"><History size={14} />History ({revisions.length} revision{revisions.length === 1 ? '' : 's'})</summary>
          <ul className="mt-2 space-y-1 font-mono text-xs">
            {[...revisions].reverse().map(item => <li key={item.revision}>r{item.revision} · {item.source} · {formatTime(item.created_at)}</li>)}
          </ul>
        </details>
      )}
    </div>
  );
}
