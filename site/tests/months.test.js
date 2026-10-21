import test from 'node:test';
import assert from 'node:assert/strict';
import { addMonths, isSelectedMonth, isValidMonth, monthBounds, monthOf, monthWeeks, monthsOfYear, weekMonths } from '../src/dates.js';
import { buildMonthRows, summarizeDay } from '../src/monthModel.js';

test('addMonths rolls over year boundaries in both directions', () => {
  assert.equal(addMonths('2026-12', 1), '2027-01');
  assert.equal(addMonths('2027-01', -1), '2026-12');
  assert.equal(addMonths('2026-10', 14), '2027-12');
  assert.equal(addMonths('2026-01', -13), '2024-12');
  assert.equal(addMonths('2026-05', 0), '2026-05');
});

test('monthBounds handles month lengths and leap years', () => {
  assert.deepEqual(monthBounds('2026-10'), { first: '2026-10-01', last: '2026-10-31' });
  assert.equal(monthBounds('2026-02').last, '2026-02-28');
  assert.equal(monthBounds('2028-02').last, '2028-02-29');
  assert.equal(monthBounds('2026-12').last, '2026-12-31');
});

test('monthWeeks lists every Monday-start week overlapping the month', () => {
  assert.deepEqual(monthWeeks('2026-10'), ['2026-09-28', '2026-10-05', '2026-10-12', '2026-10-19', '2026-10-26']);
  assert.deepEqual(monthWeeks('2026-11'), ['2026-10-26', '2026-11-02', '2026-11-09', '2026-11-16', '2026-11-23', '2026-11-30']);
  assert.equal(monthWeeks('2026-11').length, 6);
  assert.deepEqual(monthWeeks('2026-02'), ['2026-01-26', '2026-02-02', '2026-02-09', '2026-02-16', '2026-02-23']);
  // Year rollover: the week of Dec 28 belongs to both December and January.
  assert.equal(monthWeeks('2026-12').at(-1), '2026-12-28');
  assert.equal(monthWeeks('2027-01')[0], '2026-12-28');
});

test('a week crossing a month boundary appears in both months', () => {
  assert.deepEqual(weekMonths('2026-10-26'), ['2026-10', '2026-11']);
  assert.deepEqual(weekMonths('2026-12-28'), ['2026-12', '2027-01']);
  assert.deepEqual(weekMonths('2026-10-12'), ['2026-10']);
  for (const week of ['2026-10-26', '2026-12-28']) for (const month of weekMonths(week)) assert.ok(monthWeeks(month).includes(week));
});

test('month validation, selection and listing helpers', () => {
  assert.ok(isValidMonth('2026-10')); assert.ok(!isValidMonth('2026-13')); assert.ok(!isValidMonth('2026-1')); assert.ok(!isValidMonth(null));
  assert.equal(monthOf('2026-10-31'), '2026-10');
  assert.ok(isSelectedMonth('2026-11', ['2026-10', '2026-11']));
  assert.ok(!isSelectedMonth('2026-12', ['2026-10', '2026-11']));
  assert.ok(!isSelectedMonth('2026-12', undefined));
  assert.equal(monthsOfYear(2027)[0], '2027-01'); assert.equal(monthsOfYear(2027).length, 12);
});

const slot = (date, meal, recipe_id, recipe_name) => ({ date, meal, recipe_id, recipe_name, protein: 'chicken' });
const plan = { week_start: '2026-10-26', slots: [slot('2026-10-26', 'lunch', 'a', 'Arroz'), slot('2026-10-26', 'dinner', 'a', 'Arroz'), slot('2026-10-27', 'lunch', 'b', 'Sopa'), slot('2026-10-27', 'dinner', 'c', 'Pasta')] };

test('summarizeDay merges same-recipe lunch and dinner and keeps order', () => {
  assert.deepEqual(summarizeDay(plan, '2026-10-26'), [{ label: 'L·D', name: 'Arroz', protein: 'chicken' }]);
  assert.deepEqual(summarizeDay(plan, '2026-10-27').map(m => m.label + m.name), ['LSopa', 'DPasta']);
  assert.deepEqual(summarizeDay(plan, '2026-10-28'), []);
  assert.deepEqual(summarizeDay(null, '2026-10-28'), []);
});

test('buildMonthRows marks out-of-month days, weekends and missing plans', () => {
  const rows = buildMonthRows('2026-11', new Map([['2026-10-26', plan]]));
  assert.equal(rows.length, 6);
  assert.equal(rows[0].plan, plan); assert.equal(rows[1].plan, null);
  assert.deepEqual(rows[0].days.map(d => d.inMonth), [false, false, false, false, false, false, true]);
  assert.deepEqual(rows[0].days.map(d => d.weekend), [false, false, false, false, false, true, true]);
  assert.equal(rows[0].days[0].meals.length, 1); // meals of dimmed days are still shown
  assert.equal(rows[5].weekStart, '2026-11-30'); assert.equal(rows[5].days[0].date, '2026-11-30');
  assert.equal(rows[5].days[1].inMonth, false); // Dec 1 is outside Nov
});
