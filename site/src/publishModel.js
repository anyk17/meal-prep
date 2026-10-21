// Pure logic for the Publish view (home/API mode only): month choices, preview markers, job status and polling.
import { addMonths, isValidMonth } from './dates.js';

export const MAX_MONTHS = 12;
export const POLL_INTERVAL_MS = 10_000;
export const POLL_TIMEOUT_MS = 10 * 60 * 1000;

// Months offered for selection: two back to four ahead of the current month.
export function monthChoices(currentMonth, back = 2, ahead = 4) {
  const months = [];
  for (let n = -back; n <= ahead; n += 1) months.push(addMonths(currentMonth, n));
  return months;
}
export function defaultMonths(currentMonth) { return [currentMonth, addMonths(currentMonth, 1)]; }
// Toggles one month in a selection, keeping it sorted, valid and within the server limit.
export function toggleMonth(selected, month) {
  if (!isValidMonth(month)) return selected;
  if (selected.includes(month)) return selected.filter(item => item !== month);
  if (selected.length >= MAX_MONTHS) return selected;
  return [...selected, month].sort();
}
export const monthsParam = months => [...months].sort().join(',');

// 'new' = never published, 'changed' = a newer revision than the last published one, 'unchanged' = same revision.
export function weekMarker(week) {
  if (week?.published_revision === null || week?.published_revision === undefined) return 'new';
  return week.revision > week.published_revision ? 'changed' : 'unchanged';
}
export const MARKER_LABEL = { new: 'New', changed: 'Changed since last publish', unchanged: 'Unchanged' };

export function previewSummary(preview) {
  const counts = { new: 0, changed: 0, unchanged: 0 };
  for (const week of preview?.weeks ?? []) counts[weekMarker(week)] += 1;
  return { ...counts, weeks: preview?.weeks?.length ?? 0, trips: preview?.trips?.length ?? 0 };
}

export const isTerminal = job => job?.status === 'live' || job?.status === 'failed' || job?.status === 'unchanged';
export const isBusy = job => job?.status === 'running' || job?.status === 'pushed';

export function canPublishNow({ selected, preview, previewing, publishing, job }) {
  return selected.length > 0 && !!preview && !previewing && !publishing && !isBusy(job) && preview.unchanged !== true && (preview.weeks?.length ?? 0) > 0;
}

export function jobStatusText(job) {
  if (!job) return '';
  switch (job.status) {
    case 'running': return 'Publishing…';
    case 'pushed': return 'Pushed to GitHub. Waiting for the site to go live (this usually takes a minute or two).';
    case 'live': return 'Live. The public site now shows this publication.';
    case 'failed': return `Publishing failed${job.error ? `: ${job.error}` : '.'} The previous public version is still live.`;
    case 'unchanged': return 'Nothing changed since the last publication, so nothing was published.';
    default: return String(job.status);
  }
}

// Decides what the poller does next: 'stop' on a terminal job, 'timeout' after the limit, otherwise 'poll'.
export function pollDecision(job, elapsedMs, timeoutMs = POLL_TIMEOUT_MS) {
  if (!job || isTerminal(job)) return 'stop';
  if (elapsedMs >= timeoutMs) return 'timeout';
  return 'poll';
}

const ERROR_TEXT = {
  publish_in_progress: 'Another publish is running. Wait for it to finish, then try again.',
  publishing_disabled: 'Publishing is not set up on this server.',
  invalid_months: 'Choose between one and twelve valid months.',
  publish_failed: 'Publishing failed. The previous public version is still live.',
  forbidden: 'Publishing is only available from the home network.',
};
export function publishErrorMessage(error) {
  const base = ERROR_TEXT[error?.code];
  if (base) return error.code === 'publish_failed' && error.message ? `${base} ${error.message}` : base;
  return error?.message || 'Publishing could not be completed.';
}

export function lastPublishedText(publishing, format = value => value) {
  const job = publishing?.last_live_job;
  if (!job) return 'Nothing has been published yet.';
  const when = job.deployed_at || job.finished_at;
  return `Last published ${when ? format(when) : 'earlier'}: ${job.months.join(', ')}.`;
}
