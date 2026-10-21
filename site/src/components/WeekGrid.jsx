import { MealCard } from './MealCard';
import { Sun } from 'lucide-react';

// Map shorthand days to full names
const DAYS = {
  mon: 'Monday',
  tue: 'Tuesday',
  wed: 'Wednesday',
  thu: 'Thursday',
  fri: 'Friday',
  sat: 'Saturday',
  sun: 'Sunday'
};

const MEALS = ['lunch', 'dinner'];
const WEEKEND_DAYS = new Set(['sat', 'sun']);
const WEEKEND_COPY = {
  sat: {
    badge: 'Saturday',
    title: 'Eat outside',
    description: 'No cooking planned. Leave room for a lunch out, a casual dinner, or whatever sounds good that day.'
  },
  sun: {
    badge: 'Sunday',
    title: 'Kitchen off',
    description: 'Recovery day. Keep it flexible with leftovers, takeout, or a spontaneous plan before the next prep cycle.'
  }
};

export function WeekGrid({ plan, onSelect, highlightIds, onSwap = null, today = null }) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-6">
      {Object.keys(DAYS).map(day => (
        <div key={day} className="bg-white dark:bg-slate-900 rounded-xl shadow-sm border border-slate-200 dark:border-slate-800 overflow-hidden flex flex-col h-full transition-colors duration-200">
          <div className="bg-slate-50 dark:bg-slate-800/50 px-4 py-3 border-b border-slate-100 dark:border-slate-800 flex items-center justify-between">
            <span className="font-bold text-slate-700 dark:text-slate-200">{DAYS[day]}</span>
            <span className="text-xs text-slate-400 uppercase font-mono">{day}</span>
          </div>

          <div className="p-4 space-y-4 flex-1 flex flex-col justify-center">
            {WEEKEND_DAYS.has(day) ? (
              <div className="flex-1 rounded-2xl border border-dashed border-amber-300/80 dark:border-amber-700/60 bg-gradient-to-br from-amber-50 via-orange-50 to-rose-50 dark:from-amber-950/40 dark:via-orange-950/30 dark:to-rose-950/30 p-5 flex flex-col justify-between min-h-[220px]">
                <div className="flex items-center justify-between gap-3">
                  <span className="text-[11px] font-semibold uppercase tracking-[0.24em] text-amber-700 dark:text-amber-300">
                    {WEEKEND_COPY[day].badge}
                  </span>
                  <div className="rounded-full bg-white/80 dark:bg-slate-900/60 p-2 text-amber-500 dark:text-amber-300 shadow-sm">
                    <Sun size={18} />
                  </div>
                </div>

                <div className="space-y-3">
                  <h3 className="text-xl font-bold text-slate-900 dark:text-white">
                    {WEEKEND_COPY[day].title}
                  </h3>
                  <p className="text-sm leading-6 text-slate-600 dark:text-slate-300">
                    {WEEKEND_COPY[day].description}
                  </p>
                </div>

              </div>
            ) : (
              MEALS.map(meal => {
                const dto = plan.slots.find(s => s.day === day && s.meal === meal);
                if (!dto) return null;
                const slot = { ...dto, recipeName: dto.recipe_name, proteinQty: dto.protein_quantity.value, proteinUnit: dto.protein_quantity.unit, carb: dto.carb ?? 'none', carbQty: dto.carb_quantity?.value, carbUnit: dto.carb_quantity?.unit, ingredients: dto.ingredients.map(i => ({ ...i, qty: i.quantity })) };

                return (
                  <MealCard
                    key={`${day}-${meal}`}
                    slot={slot}
                    highlighted={highlightIds?.has(dto.assignment_id)}
                    onClick={() => onSelect(slot)}
                    onSwap={onSwap && today && dto.date >= today ? () => onSwap(slot) : null}
                  />
                );
              })
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
