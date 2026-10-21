import { addDays } from './readers/shared.js';
import { monthOf, monthWeeks } from './dates.js';

const DAY_KEYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];

// Compact per-day meal summary from a full plan: lunch/dinner names, merged when both meals use one recipe.
export function summarizeDay(plan, date) {
  const slots = plan ? plan.slots.filter(slot => slot.date === date) : [];
  const lunch = slots.find(slot => slot.meal === 'lunch'), dinner = slots.find(slot => slot.meal === 'dinner');
  if (lunch && dinner && lunch.recipe_id === dinner.recipe_id) return [{ label: 'L·D', name: lunch.recipe_name, protein: lunch.protein }];
  return [lunch && { label: 'L', name: lunch.recipe_name, protein: lunch.protein }, dinner && { label: 'D', name: dinner.recipe_name, protein: dinner.protein }].filter(Boolean);
}

// One row per Monday-start week that overlaps `month`. `plansByWeek` maps week_start to a full plan; weeks without a plan have plan: null.
export function buildMonthRows(month, plansByWeek) {
  return monthWeeks(month).map(weekStart => {
    const plan = plansByWeek.get(weekStart) ?? null;
    const days = DAY_KEYS.map((day, i) => {
      const date = addDays(weekStart, i);
      return { day, date, inMonth: monthOf(date) === month, weekend: i >= 5, meals: summarizeDay(plan, date) };
    });
    return { weekStart, weekEnd: addDays(weekStart, 6), plan, days };
  });
}
