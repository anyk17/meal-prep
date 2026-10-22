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

// "What to buy" choices for adding weeks to a list.
export const WHAT_TO_BUY = [
  { value: 'both', label: 'Everything', groups: ['long_lasting', 'fresh'] },
  { value: 'long_lasting', label: 'Long-lasting only', groups: ['long_lasting'] },
  { value: 'fresh', label: 'Fresh only', groups: ['fresh'] },
];
export const groupsForChoice = choice => [...(WHAT_TO_BUY.find(entry => entry.value === choice) ?? WHAT_TO_BUY[0]).groups];

// Can a week (WeekCoverage entry) take `groups` onto the list `targetTripId` (null = a new list)?
// blocked: chosen groups already on another list; useful: chosen groups not yet on the target list.
export function weekAvailability(weekCoverage, groups, targetTripId = null) {
  const blocked = [], present = [];
  for (const group of groups) {
    const entry = weekCoverage?.groups?.[group];
    if (!entry || entry.coverage === 'not_covered') continue;
    if (targetTripId && entry.trip_id === targetTripId) present.push(group); else blocked.push(group);
  }
  const useful = groups.filter(group => !present.includes(group) && !blocked.includes(group));
  return {
    blocked, useful, ok: blocked.length === 0 && useful.length > 0,
    reason: blocked.length ? `${blocked.map(groupLabel).join(' and ')} already on another shopping list` : '',
  };
}

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

