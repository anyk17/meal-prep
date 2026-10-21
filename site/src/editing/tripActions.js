// Home-only shopping trip write helpers (loaded lazily with the editor; absent from the static build).

const MESSAGES = {
  stale_version: 'This shopping list changed since you opened it (another tab or device?). It was reloaded, so check it and try again.',
  week_already_on_trip: 'That week is already on a shopping list. Remove it from that list first.',
  trip_done: 'This shopping list is done. Reopen it to change its weeks.',
  trip_not_done: 'This shopping list is not done. It was reloaded.',
  last_week: 'A list needs at least one week. Delete the list instead.',
  week_not_on_trip: 'That week is no longer on this list. It was reloaded.',
  trip_not_found: 'This shopping list no longer exists.',
  plan_not_found: 'That week no longer has a saved plan.',
  forbidden: 'Shopping lists can only be changed from the home network.',
  read_only: 'Shopping lists need the database-backed home server.',
};
const REFETCH = new Set(['stale_version', 'trip_done', 'trip_not_done', 'last_week', 'week_not_on_trip']);

export function tripProblemMessage(error) { return MESSAGES[error?.code] || error?.message || 'The shopping list could not be changed.'; }

// Runs one trip write. Resolves to { trip } on success, { deleted: true } after a delete, or { message, trip?, gone? } on a handled problem.
// On a stale version (or a state conflict) the trip is refetched so the UI shows what the server has now.
export async function runTripAction(action, { reader, tripId }) {
  try {
    const result = await action();
    return result ? { trip: result } : { deleted: true };
  } catch (error) {
    const message = tripProblemMessage(error);
    if (error?.code === 'trip_not_found') return { message, gone: true };
    if (tripId && REFETCH.has(error?.code)) {
      try { return { message, trip: await reader.getShoppingTrip(tripId) }; }
      catch (refetchError) { return refetchError?.code === 'trip_not_found' ? { message: MESSAGES.trip_not_found, gone: true } : { message: `${message} (Reloading failed: ${refetchError?.message || 'unknown error'})` }; }
    }
    return { message, code: error?.code, details: error?.details };
  }
}
