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
