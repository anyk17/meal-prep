// Pure helpers for shopping trips: grouping, ordering, and device-local checkmark storage keys.

// Checkmarks are scoped to mode, trip identity and trip version, so an edited trip starts with clean boxes.
export function tripStorageKey(mode, trip) { return `trip_checked:${mode}:${trip.trip_id}:v${trip.version}`; }
// An item is identified by ingredient and unit (the same ingredient in two units is two lines).
export function itemCheckKey(item) { return `${item.ingredient_id}|${item.unit}`; }

export function readTripChecks(storageKey) {
  try {
    const saved = window.localStorage.getItem(storageKey);
    const parsed = saved ? JSON.parse(saved) : {};
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
  } catch { return {}; }
}
export function writeTripChecks(storageKey, values) {
  try { window.localStorage.setItem(storageKey, JSON.stringify(values)); } catch { /* browser storage may be disabled */ }
}

// Sections in first-seen order. Within a section unchecked items come first, then checked ones, otherwise in original order.
export function groupItems(items, checks = {}) {
  const sections = new Map();
  for (const item of items) {
    if (!sections.has(item.section)) sections.set(item.section, []);
    sections.get(item.section).push(item);
  }
  return [...sections].map(([section, list]) => {
    const unchecked = list.filter(item => !checks[itemCheckKey(item)]);
    const checked = list.filter(item => checks[itemCheckKey(item)]);
    return { section, items: [...unchecked, ...checked], checked: checked.length, total: list.length };
  });
}
export function countChecked(items, checks) { return items.filter(item => checks[itemCheckKey(item)]).length; }

// Open lists first (soonest week first), then done lists (most recent first).
export function sortTrips(trips) {
  const first = trip => trip.weeks[0].week_start;
  const open = trips.filter(trip => trip.status === 'open').sort((a, b) => first(a).localeCompare(first(b)));
  const done = trips.filter(trip => trip.status === 'done').sort((a, b) => first(b).localeCompare(first(a)));
  return [...open, ...done];
}

export function formatQuantity(value, unit) {
  const rounded = Math.round(value * 10) / 10;
  const text = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(rounded);
  return unit === 'units' ? `${text} ${rounded === 1 ? 'unit' : 'units'}` : `${text} ${unit}`;
}

export const COVERAGE_LABEL = { not_covered: 'Not covered', on_list: 'On a list', covered: 'Covered' };

// ---- Long-lasting / fresh groups (phase 5b) ----
export const SHELF_LIVES = ['long_lasting', 'fresh'];
export const GROUP_LABEL = { long_lasting: 'Long-lasting', fresh: 'Fresh' };
export const groupLabel = group => GROUP_LABEL[group] ?? String(group);

// Items split by shelf life: Long-lasting first, then Fresh, each with the usual sections. Empty groups are left out.
export function groupItemsByShelfLife(items, checks = {}) {
  return SHELF_LIVES
    .map(group => {
      const own = items.filter(item => item.shelf_life === group);
      return { group, label: groupLabel(group), items: own, sections: groupItems(own, checks), checked: countChecked(own, checks), total: own.length };
    })
    .filter(entry => entry.total > 0);
}

// "Long-lasting + Fresh", or "Fresh only" when a week covers a single group.
export function weekGroupsLabel(groups) {
  const ordered = SHELF_LIVES.filter(group => groups?.includes(group));
  if (ordered.length === SHELF_LIVES.length) return ordered.map(groupLabel).join(' + ');
  return ordered.length ? `${groupLabel(ordered[0])} only` : '';
}

// One WeekCoverage entry as text: "Long-lasting: covered · Fresh: not covered".
export function coverageSummary(weekCoverage) {
  if (!weekCoverage?.groups) return '';
  return SHELF_LIVES.map(group => `${groupLabel(group)}: ${COVERAGE_LABEL[weekCoverage.groups[group].coverage].toLowerCase()}`).join(' · ');
}

// Groups of a week (WeekCoverage entry) that are on no list at all, in display order.
export const freeGroups = weekCoverage => SHELF_LIVES.filter(group => weekCoverage?.groups?.[group]?.coverage === 'not_covered');

// The other list a group sits on: its name from `tripNames` (Map trip_id -> name), or null when unknown.
function otherListName(weekCoverage, group, tripNames) {
  const id = weekCoverage?.groups?.[group]?.trip_id;
  return (id && (tripNames instanceof Map ? tripNames.get(id) : tripNames?.[id])) || null;
}

// What adding a week would do: the free groups to add, and the groups skipped because another list has them.
export function addPlan(weekCoverage, tripNames) {
  const groups = freeGroups(weekCoverage);
  const skipped = SHELF_LIVES.filter(group => !groups.includes(group) && weekCoverage?.groups?.[group]?.coverage)
    .map(group => ({ group, tripName: otherListName(weekCoverage, group, tripNames) }));
  return { groups, skipped };
}
const listText = skip => (skip.tripName ? `on '${skip.tripName}'` : 'on another list');

// "Added Nov 2 – Nov 8 (Fresh only — Long-lasting is on 'X')"; plain "Added Nov 2 – Nov 8" when nothing was skipped.
export function addedMessage(weekLabel, groups, skipped = []) {
  if (!skipped.length) return `Added ${weekLabel}`;
  return `Added ${weekLabel} (${weekGroupsLabel(groups)} — ${skipped.map(skip => `${groupLabel(skip.group)} is ${listText(skip)}`).join(', ')})`;
}

// Edit-chip state for one group of a week already on `tripId`:
//   on: the group is on this list; off-free: nobody has it (can be added);
//   off-blocked: another list has it (otherList = its name or null); off-unknown: no coverage info yet.
export function chipState({ tripWeek, coverage, group, tripId, tripNames }) {
  if (tripWeek?.groups?.includes(group)) return { state: 'on', otherList: null };
  const entry = coverage?.groups?.[group];
  if (!entry) return { state: 'off-unknown', otherList: null };
  if (entry.coverage === 'not_covered') return { state: 'off-free', otherList: null };
  if (entry.trip_id === tripId) return { state: 'on', otherList: null };
  return { state: 'off-blocked', otherList: otherListName(coverage, group, tripNames) };
}

export const LAST_WEEK_HINT = 'A list needs at least one week. Delete it instead.';

// What tapping an ON chip does: drop just that group, or the whole week when it is the week's only group.
// Dropping the last group of the last week would empty the list, so that is blocked.
export function chipRemoval(trip, tripWeek, group) {
  const only = tripWeek.groups.length < 2 || !tripWeek.groups.includes(group);
  if (only && trip.weeks.length < 2) return { kind: 'blocked', hint: LAST_WEEK_HINT };
  return only ? { kind: 'remove_week' } : { kind: 'remove_group', group };
}
// The week's ✕ button: removing the only week is blocked.
export const weekRemoval = trip => (trip.weeks.length < 2 ? { kind: 'blocked', hint: LAST_WEEK_HINT } : { kind: 'remove_week' });

// Candidate weeks for a list's "Add a week": not already on it, with at least one free group.
export const addableWeeks = (candidates, trip) => (candidates ?? []).filter(item => !trip.weeks.some(week => week.plan_id === item.plan_id) && freeGroups(item).length > 0);

// Can anything at all still be added for this week? (Used to enable the month-view checkbox.)
export const weekSelectable = weekCoverage => SHELF_LIVES.some(group => weekCoverage?.groups?.[group]?.coverage === 'not_covered');

// Request bodies / paths for shopping-list writes (kept pure so they are testable).
export function createTripBody(planIds, name, groups) {
  const body = { plan_ids: planIds };
  if (typeof name === 'string' && name.trim()) body.name = name.trim().slice(0, 80);
  if (groups) body.groups = normalizeGroups(groups);
  return body;
}
export function addWeekBody(planId, expectedVersion, groups) {
  const body = { plan_id: planId, expected_version: expectedVersion };
  if (groups) body.groups = normalizeGroups(groups);
  return body;
}
export function removeWeekPath(tripId, planId, expectedVersion, group) {
  const query = `expected_version=${expectedVersion}${group ? `&group=${encodeURIComponent(group)}` : ''}`;
  return `/shopping-trips/${encodeURIComponent(tripId)}/weeks/${encodeURIComponent(planId)}?${query}`;
}
export function normalizeGroups(groups) {
  if (!Array.isArray(groups) || !groups.length || groups.some(group => !SHELF_LIVES.includes(group))) throw new Error('Choose long-lasting, fresh, or both.');
  return SHELF_LIVES.filter(group => groups.includes(group));
}

// Merge a swap preview's trip impact into one entry per list. Accepts one object or an array, each with optional `groups`
// (a string or array) or one entry per (list, group).
export function swapImpactEntries(impact) {
  const list = impact == null ? [] : Array.isArray(impact) ? impact : [impact];
  const byTrip = new Map();
  for (const entry of list) {
    if (!entry) continue;
    const key = entry.trip_id ?? entry.name;
    const groups = entry.groups ?? (entry.group ? [entry.group] : entry.shelf_life ? [entry.shelf_life] : []);
    const known = byTrip.get(key);
    if (!known) byTrip.set(key, { ...entry, groups: [...groups], extra_needed: [...(entry.extra_needed ?? [])] });
    else {
      known.groups = [...new Set([...known.groups, ...groups])];
      known.extra_needed.push(...(entry.extra_needed ?? []));
    }
  }
  for (const entry of byTrip.values()) entry.groups = SHELF_LIVES.filter(group => entry.groups.includes(group));
  return [...byTrip.values()];
}

// Adds each group's list name (from the trip list) to a WeekCoverage entry.
export function decorateCoverage(entry, trips) {
  if (!entry) return null;
  const names = new Map((trips ?? []).map(trip => [trip.trip_id, trip.name]));
  return { ...entry, groups: Object.fromEntries(SHELF_LIVES.map(group => [group, { ...entry.groups[group], trip_name: names.get(entry.groups[group].trip_id) ?? null }])) };
}

