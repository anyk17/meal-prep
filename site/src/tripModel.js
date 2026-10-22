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

export const COVERAGE_LABEL = { not_covered: 'Not covered', to_buy: 'To buy', bought: 'Bought' };

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

export const LAST_WEEK_HINT = 'A list needs at least one week. Delete it instead.';

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
    // Groups arrive as names or as cells ({group, state}).
    const groups = (entry.groups ?? (entry.group ? [entry.group] : entry.shelf_life ? [entry.shelf_life] : [])).map(group => (typeof group === 'string' ? group : group?.group));
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


// ---- Cells: a (week, group) on a list is to_buy or bought (phase 5c) ----
export const CELL_STATES = ['to_buy', 'bought'];
export const cellKey = (planId, group) => `${planId}|${group}`;
export const cellStateLabel = state => COVERAGE_LABEL[state] ?? String(state);

// The cell of one group in a trip week, or null.
export const cellOf = (week, group) => week?.cells?.find(cell => cell.group === group) ?? null;
export const cellAt = (trip, planId, group) => cellOf(trip?.weeks?.find(week => week.plan_id === planId), group);

// Every cell of a trip, weeks in order and groups long_lasting then fresh, each with its week identity and a stable key.
export function tripCells(trip) {
  return (trip?.weeks ?? []).flatMap(week => (week.cells ?? []).map(cell => ({ ...cell, plan_id: week.plan_id, week_start: week.week_start, week_end: week.week_end, current_revision: week.current_revision, key: cellKey(week.plan_id, cell.group) })));
}
export const toBuyCells = trip => tripCells(trip).filter(cell => cell.state === 'to_buy');
export const boughtCells = trip => tripCells(trip).filter(cell => cell.state === 'bought');
export const weekChanged = week => (week?.cells ?? []).some(cell => cell.changed_since_shopping);
export const tripChanged = trip => (trip?.weeks ?? []).some(weekChanged);
export const nothingToBuy = trip => toBuyCells(trip).length === 0;

// "Long-lasting: bought · Fresh: to buy" for the groups of one trip week.
export function cellsSummary(week) {
  return (week?.cells ?? []).map(cell => `${groupLabel(cell.group)}: ${cellStateLabel(cell.state).toLowerCase()}`).join(' · ');
}

// Sum quantity_by_plan over the ticked cells of the item's own group. Items with nothing left are dropped.
// `selected` is a Set (or array) of cell keys; the result keeps the item shape with `quantity` replaced.
export function totalsForCells(items, selected) {
  const keys = selected instanceof Set ? selected : new Set(selected ?? []);
  const out = [];
  for (const item of items ?? []) {
    let quantity = 0;
    for (const [planId, value] of Object.entries(item.quantity_by_plan ?? {})) if (keys.has(cellKey(planId, item.shelf_life))) quantity += value;
    if (quantity > 0) out.push({ ...item, quantity: Math.round(quantity * 1e6) / 1e6 });
  }
  return out;
}

// Which to-buy cells are ticked. Only the UNticked keys are stored, so the default is "everything" and new cells come ticked.
export const selectionStorageKey = (mode, trip) => `trip_cells:${mode}:${trip.trip_id}`;
export function readUnselected(storageKey) {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(storageKey) ?? '[]');
    return Array.isArray(parsed) ? parsed.filter(value => typeof value === 'string') : [];
  } catch { return []; }
}
export function writeUnselected(storageKey, keys) {
  try { window.localStorage.setItem(storageKey, JSON.stringify([...keys])); } catch { /* browser storage may be disabled */ }
}
// Ticked = to-buy cells not in the unticked list. Returns the cells in trip order.
export const selectedCells = (trip, unselected) => { const off = new Set(unselected); return toBuyCells(trip).filter(cell => !off.has(cell.key)); };
export function toggleUnselected(unselected, key) { const set = new Set(unselected); if (set.has(key)) set.delete(key); else set.add(key); return [...set]; }

// Short text for a cell/group set: "Nov 2 – Nov 8 · Fresh".
export const cellLabel = (cell, formatWeek) => `${formatWeek(cell.week_start)} · ${groupLabel(cell.group)}`;

// Body for POST .../purchases and .../purchases/undo.
export function purchaseBody(expectedVersion, cells) {
  if (!Array.isArray(cells) || !cells.length) throw new Error('Choose at least one week and group.');
  return { expected_version: expectedVersion, cells: cells.map(cell => ({ plan_id: cell.plan_id, group: cell.group })) };
}
