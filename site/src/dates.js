import { addDays } from './readers/shared.js';

export function mondayOf(date) {
  const day = new Date(`${date}T00:00:00Z`).getUTCDay() || 7;
  return addDays(date, 1 - day);
}
// Today's calendar date (YYYY-MM-DD) in a timezone; falls back to the browser's zone.
export function todayIn(timeZone) {
  try { return new Intl.DateTimeFormat('en-CA', { timeZone, year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date()); }
  catch { return new Intl.DateTimeFormat('en-CA', { year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date()); }
}
export function formatWeekRange(weekStart) {
  const fmt = (value, withYear) => new Intl.DateTimeFormat(undefined, { timeZone: 'UTC', month: 'short', day: 'numeric', ...(withYear ? { year: 'numeric' } : {}) }).format(new Date(`${value}T00:00:00Z`));
  return `${fmt(weekStart, false)} – ${fmt(addDays(weekStart, 6), true)}`;
}
export function formatMonth(month) {
  return new Intl.DateTimeFormat(undefined, { timeZone: 'UTC', month: 'long', year: 'numeric' }).format(new Date(`${month}-01T00:00:00Z`));
}

// ---- Month helpers (a month is 'YYYY-MM'; all arithmetic is calendar-only, in UTC, with no timezone drift) ----
export function monthOf(date) { return date.slice(0, 7); }
export function isValidMonth(value) { return typeof value === 'string' && /^\d{4}-(0[1-9]|1[0-2])$/.test(value) && !value.startsWith('0000'); }
export function addMonths(month, n) {
  const [year, mon] = month.split('-').map(Number);
  const index = year * 12 + (mon - 1) + n;
  return `${String(Math.floor(index / 12)).padStart(4, '0')}-${String((index % 12) + 1).padStart(2, '0')}`;
}
export function monthBounds(month) {
  return { first: `${month}-01`, last: addDays(`${addMonths(month, 1)}-01`, -1) };
}
// Monday-start weeks that overlap the month, in order (4 to 6 weeks). A boundary week appears in both neighbouring months.
export function monthWeeks(month) {
  const { first, last } = monthBounds(month);
  const weeks = [];
  for (let week = mondayOf(first); week <= last; week = addDays(week, 7)) weeks.push(week);
  return weeks;
}
// The one or two months a Monday-start week overlaps.
export function weekMonths(weekStart) {
  const start = monthOf(weekStart), end = monthOf(addDays(weekStart, 6));
  return start === end ? [start] : [start, end];
}
export function isSelectedMonth(month, selectedMonths) { return Array.isArray(selectedMonths) && selectedMonths.includes(month); }
export function monthsOfYear(year) { return Array.from({ length: 12 }, (_, i) => `${String(year).padStart(4, '0')}-${String(i + 1).padStart(2, '0')}`); }
