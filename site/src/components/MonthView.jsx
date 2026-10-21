import { Suspense, useEffect, useState } from 'react';
import { CalendarDays, ChevronLeft, ChevronRight, Moon } from 'lucide-react';
import { cn } from '../lib/utils';
import { addMonths, formatMonth, formatWeekRange, isSelectedMonth, monthBounds, monthsOfYear } from '../dates.js';
import { buildMonthRows } from '../monthModel.js';

const WEEKDAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const nav = 'p-2 rounded-lg bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 disabled:opacity-50';
const control = 'px-3 py-2 rounded-lg bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 text-sm disabled:opacity-50';
const monthName = month => new Intl.DateTimeFormat(undefined, { timeZone: 'UTC', month: 'long' }).format(new Date(`${month}-01T00:00:00Z`));

async function loadMonth(reader, month) {
  const { first, last } = monthBounds(month);
  const { items } = await reader.listPlans({ from: first, to: last });
  const context = reader.context;
  // Static: only months in the pinned publication count as published (listPlans has already loaded it).
  const published = reader.mode !== 'static' || isSelectedMonth(month, context?.selected_months);
  if (!published) return { published, rows: [], selectedMonths: context?.selected_months };
  const plans = await Promise.all(items.map(item => reader.getPlan(item.plan_id)));
  return { published, rows: buildMonthRows(month, new Map(plans.map(plan => [plan.week_start, plan]))), selectedMonths: context?.selected_months };
}

function MonthNav({ month, onMonth, thisMonth, selectedMonths, staticMode }) {
  const [year, mon] = month.split('-');
  const years = new Set([Number(year) - 2, Number(year) - 1, Number(year), Number(year) + 1, Number(year) + 2, Number(thisMonth.slice(0, 4))]);
  for (const published of selectedMonths ?? []) years.add(Number(published.slice(0, 4)));
  const tag = value => staticMode && isSelectedMonth(value, selectedMonths) ? ' (published)' : '';
  return (
    <nav aria-label="Month navigation" className="flex flex-wrap items-center gap-2">
      <button onClick={() => onMonth(addMonths(month, -12))} aria-label="Previous year" className={cn(nav, 'text-xs font-mono')}>−1y</button>
      <button onClick={() => onMonth(addMonths(month, -1))} aria-label="Previous month" className={nav}><ChevronLeft size={16} /></button>
      <select aria-label="Month" value={mon} onChange={event => onMonth(`${year}-${event.target.value}`)} className={control}>
        {monthsOfYear(year).map(value => <option key={value} value={value.slice(5)}>{monthName(value)}{tag(value)}</option>)}
      </select>
      <select aria-label="Year" value={year} onChange={event => onMonth(`${event.target.value}-${mon}`)} className={control}>
        {[...years].sort().map(value => <option key={value} value={String(value).padStart(4, '0')}>{value}</option>)}
      </select>
      <button onClick={() => onMonth(addMonths(month, 1))} aria-label="Next month" className={nav}><ChevronRight size={16} /></button>
      <button onClick={() => onMonth(addMonths(month, 12))} aria-label="Next year" className={cn(nav, 'text-xs font-mono')}>+1y</button>
      <button onClick={() => onMonth(thisMonth)} disabled={month === thisMonth} className={control}>This month</button>
    </nav>
  );
}

function DayCell({ day }) {
  return (
    <div className={cn('px-3 py-2 md:p-2 min-w-0 md:min-h-[5.5rem] flex md:flex-col gap-x-3 gap-y-1 items-baseline md:items-stretch border-t md:border-t-0 md:border-l border-slate-100 dark:border-slate-800 first:border-t-0 md:first:border-l-0',
      !day.inMonth && 'opacity-40 bg-slate-50 dark:bg-slate-950/40',
      day.weekend && 'bg-amber-50/60 dark:bg-amber-950/20')}>
      <div className="flex items-baseline gap-1 w-16 md:w-auto shrink-0 text-xs">
        <span className="font-semibold text-slate-600 dark:text-slate-300">{WEEKDAY_LABELS[day.weekdayIndex]}</span>
        <span className={cn('font-mono', day.inMonth ? 'text-slate-700 dark:text-slate-200 font-semibold' : 'text-slate-400')}>{Number(day.date.slice(8))}</span>
        {day.date.endsWith('-01') && <span className="text-[10px] uppercase text-slate-400">{monthName(day.date.slice(0, 7)).slice(0, 3)}</span>}
      </div>
      {day.weekend ? (
        <div className="flex items-center gap-1 text-[11px] text-amber-700 dark:text-amber-300 italic"><Moon size={11} />{day.day === 'sat' ? 'Eat outside' : 'Kitchen off'}</div>
      ) : (
        <ul className="space-y-0.5 min-w-0 flex-1">
          {day.meals.map(meal => (
            <li key={meal.label} className="text-[11px] leading-tight text-slate-700 dark:text-slate-200 flex gap-1 min-w-0">
              <span className="font-mono text-slate-400 shrink-0">{meal.label}</span><span className="truncate md:whitespace-normal md:line-clamp-2" title={meal.name}>{meal.name}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function WeekBlock({ row, month, staticMode, canEdit, onOpenWeek, renderWeekAccessory }) {
  const crossing = row.days.some(day => !day.inMonth);
  return (
    <section aria-label={`Week of ${row.weekStart}`} className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 overflow-hidden" data-week={row.weekStart}>
      <div className="px-3 py-2 bg-slate-50 dark:bg-slate-800/50 border-b border-slate-100 dark:border-slate-800 flex flex-wrap items-center gap-2">
        <button onClick={() => onOpenWeek(row.weekStart)} className="font-semibold text-sm text-indigo-700 dark:text-indigo-300 hover:underline text-left">{formatWeekRange(row.weekStart)}</button>
        {row.plan && <span className="text-[10px] font-mono text-slate-500 dark:text-slate-400">r{row.plan.revision}</span>}
        {crossing && <span className="text-[10px] text-slate-500 dark:text-slate-400">also in {row.weekStart.slice(0, 7) < month ? 'previous' : 'next'} month</span>}
        <span className="ml-auto flex items-center gap-2">{renderWeekAccessory?.(row)}</span>
      </div>
      {row.plan ? (
        <div className="md:grid md:grid-cols-7">{row.days.map((day, i) => <DayCell key={day.date} day={{ ...day, weekdayIndex: i }} />)}</div>
      ) : (
        <p className="px-4 py-5 text-sm text-slate-500 dark:text-slate-400">
          {staticMode ? 'No published plan for this week.' : canEdit ? 'No plan for this week yet. Use Generate month above, or open the week to create it.' : 'No plan for this week yet.'}
        </p>
      )}
    </section>
  );
}

export function MonthView({ reader, month, onMonth, thisMonth, nonce, canEdit, MonthFill, onOpenWeek, onReload, renderWeekAccessory }) {
  const key = `${month}:${nonce}:${reader.mode}`;
  const [state, setState] = useState({ key: null });
  const [retry, setRetry] = useState(0);
  const requestKey = `${key}:${retry}`;
  useEffect(() => {
    let live = true;
    loadMonth(reader, month)
      .then(result => { if (live) setState({ key: requestKey, ...result }); })
      .catch(err => { if (live) setState({ key: requestKey, error: err?.message || 'Could not load this month.' }); });
    return () => { live = false; };
  }, [reader, month, requestKey]);

  const staticMode = reader.mode === 'static';
  const current = state.key === requestKey ? state : null;
  const selectedMonths = current?.selectedMonths ?? reader.context?.selected_months;
  const published = staticMode && isSelectedMonth(month, selectedMonths);

  return (
    <div className="space-y-4" data-testid="month-view">
      <MonthNav month={month} onMonth={onMonth} thisMonth={thisMonth} selectedMonths={selectedMonths} staticMode={staticMode} />
      <div className="flex flex-wrap items-center gap-2">
        <CalendarDays className="text-indigo-600 dark:text-indigo-400" />
        <h2 className="text-xl font-bold text-slate-800 dark:text-white">{formatMonth(month)}</h2>
        {published && <span className="text-xs px-2 py-0.5 rounded-full bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300">Published</span>}
      </div>
      {canEdit && MonthFill && (
        <Suspense fallback={null}><MonthFill key={month} month={month} onReload={onReload} /></Suspense>
      )}
      {!current ? (
        <div className="rounded-xl border border-dashed border-slate-300 dark:border-slate-700 p-10 text-center text-slate-600 dark:text-slate-300">Loading month…</div>
      ) : current.error ? (
        <div role="alert" className="rounded-xl border border-dashed border-red-300 dark:border-red-900 p-8 text-center text-slate-700 dark:text-slate-300">
          Unable to load {formatMonth(month)}: {current.error} <button className="ml-2 underline" onClick={() => setRetry(value => value + 1)}>Retry</button>
        </div>
      ) : !current.published ? (
        <div className="rounded-xl border border-dashed border-slate-300 dark:border-slate-700 p-10 text-center text-slate-600 dark:text-slate-300 space-y-2">
          <p className="font-medium">No published plan for {formatMonth(month)}.</p>
          <p className="text-sm text-slate-500 dark:text-slate-400">The public site only shows months that were published. This does not mean there is no plan at home.</p>
        </div>
      ) : (
        <div className="space-y-4">
          {current.rows.map(row => <WeekBlock key={row.weekStart} row={row} month={month} staticMode={staticMode} canEdit={canEdit} onOpenWeek={onOpenWeek} renderWeekAccessory={renderWeekAccessory} />)}
        </div>
      )}
    </div>
  );
}
