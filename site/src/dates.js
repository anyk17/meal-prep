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
