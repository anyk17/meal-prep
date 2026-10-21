// Publish client for the home (API-mode) deployment only; loaded lazily with the Publish view and absent from the Pages build.
import { ReaderError, normalizeError } from '../readers/shared.js';
import { monthsParam } from '../publishModel.js';

export function createPublishApi(base = '/api/v1') {
  const path = base.replace(/\/$/, '');
  async function request(method, endpoint, body) {
    let response;
    try {
      response = await fetch(`${path}${endpoint}`, { method, cache: 'no-store', headers: body === undefined ? { Accept: 'application/json' } : { Accept: 'application/json', 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
    } catch { throw new ReaderError('source_unavailable', 'The meal plan API could not be reached.'); }
    let payload;
    try { payload = await response.json(); } catch {
      throw new ReaderError(response.ok ? 'source_invalid' : 'source_unavailable', response.ok ? 'The API returned invalid JSON.' : `The meal plan API could not be reached (${response.status}).`);
    }
    if (!response.ok) {
      const error = payload?.error;
      if (!error || typeof error.code !== 'string' || typeof error.message !== 'string') throw new ReaderError('source_invalid', 'The API returned an invalid error response.');
      const normalized = normalizeError(error.code, error.message, error.details);
      normalized.status = response.status;
      throw normalized;
    }
    return payload;
  }
  const checkJob = job => {
    if (!job || typeof job.job_id !== 'string' || typeof job.status !== 'string' || !Array.isArray(job.months)) throw new ReaderError('source_invalid', 'The API returned an invalid publishing job.');
    return job;
  };
  return {
    async getStatus() {
      const result = await request('GET', '/publishing');
      if (!result || typeof result.enabled !== 'boolean') throw new ReaderError('source_invalid', 'The API returned an invalid publishing status.');
      if (result.latest_job) checkJob(result.latest_job);
      if (result.last_live_job) checkJob(result.last_live_job);
      return result;
    },
    async preview(months) {
      const result = await request('GET', `/publishing/preview?months=${encodeURIComponent(monthsParam(months))}`);
      if (!result || !Array.isArray(result.weeks) || !Array.isArray(result.trips)) throw new ReaderError('source_invalid', 'The API returned an invalid publishing preview.');
      return result;
    },
    async publish(months) { return checkJob(await request('POST', '/publishing/jobs', { months: [...months].sort() })); },
    async getJob(jobId) { return checkJob(await request('GET', `/publishing/jobs/${encodeURIComponent(jobId)}`)); },
  };
}
