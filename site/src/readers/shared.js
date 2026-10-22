export class ReaderError extends Error {
  constructor(code, message, details = []) { super(message); this.name = 'ReaderError'; this.code = code; this.details = details; }
}

const fail = (message, field = '$') => { throw new ReaderError('source_invalid', message, [{ field, message }]); };
export function validDate(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith('0000')) return false;
  const d = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(d.getTime()) && d.toISOString().slice(0, 10) === value;
}
export function validateRange({ from, to }) {
  if (!validDate(from) || !validDate(to) || from > to || (Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86400000 > 365)
    throw new ReaderError('invalid_request', 'Provide a valid inclusive date range of at most 366 days.', [{ field: 'range', message: 'Invalid date range' }]);
}
const object = value => value && typeof value === 'object' && !Array.isArray(value);
const exactKeys = (value, keys) => object(value) && Object.keys(value).sort().join(',') === [...keys].sort().join(',');
const idPattern = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
function quantity(q, field) { if (!object(q) || typeof q.value !== 'number' || !Number.isFinite(q.value) || q.value < 0 || !['g', 'ml', 'units'].includes(q.unit)) fail('Invalid quantity', field); }
const summaryKeys=['plan_id','week_start','week_end','timezone','revision','snapshot_id','generated_at','seed'];
export function validateSummary(s, isPlan = false) {
  const allowed = isPlan ? [...summaryKeys,'slots','derived'] : summaryKeys;
  if (!object(s) || summaryKeys.some(key => !(key in s)) || Object.keys(s).some(key => !allowed.includes(key)) || !idPattern.test(s.plan_id || '') || !validDate(s.week_start) || !validDate(s.week_end) || s.week_end !== addDays(s.week_start, 6) || weekDay(s.week_start) !== 1 || !Number.isInteger(s.revision) || s.revision < 1 || typeof s.snapshot_id !== 'string' || !s.snapshot_id || typeof s.timezone !== 'string' || !validTimezone(s.timezone) || !Number.isInteger(s.seed) || !validTimestamp(s.generated_at)) fail('Invalid plan summary');
}
export function validatePlan(p) {
  validateSummary(p,true);
  if (!exactKeys(p,['plan_id','week_start','week_end','timezone','revision','snapshot_id','generated_at','seed','slots','derived']) || !Array.isArray(p.slots) || !exactKeys(p.derived,['protein_counts','carb_counts']) || !object(p.derived.protein_counts) || !object(p.derived.carb_counts)) fail('Invalid plan structure');
  const days = ['mon','tue','wed','thu','fri','sat','sun'], keys = new Set(), assignments = new Map(), bySlot = new Map();
  let previousSortKey = '';
  for (const s of p.slots) {
    if (!exactKeys(s,['assignment_id','date','day','meal','recipe_id','recipe_name','variant_id','protein','protein_quantity','carb','carb_quantity','ingredients','extended_from_lunch','linked_assignment_id']) || typeof s.assignment_id !== 'string' || !s.assignment_id || !validDate(s.date) || s.date < p.week_start || s.date > p.week_end || days[weekDay(s.date)-1] !== s.day || !['lunch','dinner'].includes(s.meal) || !['chicken','beef','pork','fish','egg'].includes(s.protein) || !Array.isArray(s.ingredients) || typeof s.recipe_name !== 'string' || typeof s.recipe_id !== 'string' || typeof s.variant_id !== 'string' || typeof s.extended_from_lunch !== 'boolean' || (s.linked_assignment_id !== null && typeof s.linked_assignment_id !== 'string') || (s.carb !== null && typeof s.carb !== 'string')) fail('Invalid meal assignment');
    quantity(s.protein_quantity, 'protein_quantity');
    if ((s.carb === null) !== (s.carb_quantity === null)) fail('Carb and carb_quantity must both be null or present');
    if (s.carb_quantity) quantity(s.carb_quantity, 'carb_quantity');
    const key = `${s.date}/${s.meal}`, sortKey = `${s.date}/${s.meal === 'lunch' ? '0' : '1'}`;
    if (sortKey <= previousSortKey) fail('Meal assignments must be ordered by date, then lunch before dinner');
    previousSortKey = sortKey;
    if (keys.has(key) || assignments.has(s.assignment_id)) fail('Duplicate meal assignment'); keys.add(key); assignments.set(s.assignment_id, s); bySlot.set(key,s);
    for (const i of s.ingredients) if (!exactKeys(i,['ingredient_id','display','quantity','unit','role']) || typeof i.ingredient_id !== 'string' || !i.ingredient_id || typeof i.display !== 'string' || typeof i.quantity !== 'number' || !Number.isFinite(i.quantity) || i.quantity < 0 || !['g','ml','units'].includes(i.unit) || !['protein','protein_extra','veg','fat','condiment','garnish','herb','spice','dairy','other','carb'].includes(i.role)) fail('Invalid plan ingredient');
    const proteinIngredients=s.ingredients.filter(i=>i.role==='protein');
    if (proteinIngredients.length!==1 || proteinIngredients[0].unit!==s.protein_quantity.unit || proteinIngredients[0].quantity!==s.protein_quantity.value) fail('Protein quantity does not match its recipe ingredient');
    const carbIngredients=s.ingredients.filter(i=>i.role==='carb');
    if (s.carb===null ? carbIngredients.length!==0 : carbIngredients.length!==1 || carbIngredients[0].ingredient_id!==s.carb || carbIngredients[0].quantity!==s.carb_quantity.value || carbIngredients[0].unit!==s.carb_quantity.unit) fail('Carb quantity does not match its recipe ingredient');
  }
  for (const s of p.slots) {
    if (s.linked_assignment_id) {
      const linked=assignments.get(s.linked_assignment_id);
      if (!linked || linked.linked_assignment_id !== s.assignment_id || linked.date !== s.date || linked.meal === s.meal || linked.recipe_id !== s.recipe_id) fail('Linked assignments must be reciprocal same-day recipe pairs');
    }
    if (s.extended_from_lunch && !(s.meal === 'dinner' && bySlot.get(`${s.date}/lunch`)?.recipe_id === s.recipe_id)) fail('Extended dinner must match its same-day lunch recipe');
  }
  const proteins = Object.fromEntries(['chicken','beef','pork','fish','egg'].map(key => [key, p.slots.filter(slot => slot.protein === key).length]));
  for (const [key, value] of Object.entries(proteins)) if (p.derived.protein_counts[key] !== value) fail(`Derived protein count mismatch for ${key}`);
  const carbs = {};
  for (const slot of p.slots) if (slot.carb !== null) carbs[slot.carb] = (carbs[slot.carb] || 0) + 1;
  if (Object.keys(carbs).length !== Object.keys(p.derived.carb_counts).length || Object.entries(carbs).some(([key,value]) => p.derived.carb_counts[key] !== value)) fail('Derived carb counts do not match assignments');
}
export function validateShopping(s) {
  if (!exactKeys(s,['plan_id','revision','snapshot_id','week_start','week_end','sections']) || !idPattern.test(s.plan_id || '') || !Number.isInteger(s.revision) || s.revision < 1 || typeof s.snapshot_id !== 'string' || !s.snapshot_id || !validDate(s.week_start) || !validDate(s.week_end) || s.week_end !== addDays(s.week_start,6) || !object(s.sections)) fail('Invalid shopping list');
  for (const [section,lines] of Object.entries(s.sections)) { if (!Array.isArray(lines)) fail('Shopping sections must contain arrays'); for (const l of lines) if (!exactKeys(l,['ingredient_id','display','quantity','unit','section','notes']) || typeof l.ingredient_id !== 'string' || !l.ingredient_id || typeof l.display !== 'string' || typeof l.quantity !== 'number' || !Number.isFinite(l.quantity) || l.quantity < 0 || !['g','ml','units'].includes(l.unit) || l.section !== section || (l.notes !== null && typeof l.notes !== 'string')) fail('Invalid shopping line'); }
}
export function validatePair(plan, shopping) {
  validatePlan(plan); validateShopping(shopping);
  for (const key of ['plan_id','revision','snapshot_id','week_start','week_end']) if (plan[key] !== shopping[key]) fail(`Plan and shopping list disagree on ${key}`);
}
export function addDays(date, n) { const d = new Date(`${date}T00:00:00Z`); d.setUTCDate(d.getUTCDate()+n); return d.toISOString().slice(0,10); }
function weekDay(date) { return new Date(`${date}T00:00:00Z`).getUTCDay() || 7; }
export function validTimestamp(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value) || !validDate(value.slice(0,10))) return false;
  const time = value.slice(11,19).split(':').map(Number);
  return time[0] <= 23 && time[1] <= 59 && time[2] <= 59 && Number.isFinite(Date.parse(value));
}
function validTimezone(value) { try { new Intl.DateTimeFormat('en',{timeZone:value}); return true; } catch { return false; } }
export function normalizeError(code, message, details = []) { return new ReaderError(code, message, Array.isArray(details) ? details : []); }

// ---- Shopping trips (phase 4) ----
const UNITS = ['g', 'ml', 'units'];
const tripWeekKeys = ['plan_id','week_start','week_end','current_revision','groups','cells'];
const SHELF_LIVES = ['long_lasting', 'fresh'];
// Non-empty, unique, in canonical order (long_lasting before fresh).
const validGroups = g => Array.isArray(g) && g.length > 0 && g.every(x => SHELF_LIVES.includes(x)) && new Set(g).size === g.length && g.every((x, i) => i === 0 || SHELF_LIVES.indexOf(g[i - 1]) < SHELF_LIVES.indexOf(x));
const tripKeys = ['trip_id','name','status','version','created_at','updated_at','completed_at','weeks','items','bought_items'];
const cellKeys = ['group','state','bought_at','revision','snapshot_id','changed_since_shopping'];
// One (week, group) cell. A bought cell freezes a revision and snapshot and carries its time; a to-buy cell has no time and is never "changed".
function validCell(cell, week) {
  if (!exactKeys(cell, cellKeys) || !['to_buy', 'bought'].includes(cell.state) || typeof cell.changed_since_shopping !== 'boolean') return false;
  const revisionOk = cell.revision === null || (Number.isInteger(cell.revision) && cell.revision >= 1);
  const snapshotOk = cell.snapshot_id === null || (typeof cell.snapshot_id === 'string' && cell.snapshot_id !== '');
  if (!revisionOk || !snapshotOk) return false;
  if (cell.state === 'to_buy') return cell.bought_at === null && !cell.changed_since_shopping;
  return validTimestamp(cell.bought_at) && cell.revision !== null && cell.snapshot_id !== null && cell.changed_since_shopping === (week.current_revision > cell.revision);
}
function validateTripItem(i, planIds, field) {
  const keys = ['ingredient_id','display','quantity','unit','section','shelf_life','quantity_by_plan'];
  const shape = object(i) && keys.every(key => key in i) && Object.keys(i).every(key => keys.includes(key) || key === 'plan_ids');
  if (!shape || typeof i.ingredient_id !== 'string' || !i.ingredient_id || typeof i.display !== 'string' || typeof i.quantity !== 'number' || !Number.isFinite(i.quantity) || i.quantity < 0 || !UNITS.includes(i.unit) || typeof i.section !== 'string' || !SHELF_LIVES.includes(i.shelf_life)) fail(`Invalid shopping trip item in ${field}`);
  if (!object(i.quantity_by_plan) || !Object.keys(i.quantity_by_plan).length || Object.entries(i.quantity_by_plan).some(([id, q]) => !planIds.has(id) || typeof q !== 'number' || !Number.isFinite(q) || q < 0)) fail(`Invalid quantity_by_plan in ${field}`);
  if ('plan_ids' in i && (!Array.isArray(i.plan_ids) || i.plan_ids.some(id => !planIds.has(id)))) fail(`Invalid plan_ids in ${field}`);
}
export function validateTrip(t) {
  if (!exactKeys(t, tripKeys) || !idPattern.test(t.trip_id || '') || typeof t.name !== 'string' || !t.name || t.name.length > 80 || !['open','done'].includes(t.status) || !Number.isInteger(t.version) || t.version < 1 || !validTimestamp(t.created_at) || !validTimestamp(t.updated_at) || !Array.isArray(t.weeks) || !t.weeks.length || !Array.isArray(t.items) || !Array.isArray(t.bought_items)) fail('Invalid shopping trip');
  if (t.status === 'open' ? t.completed_at !== null : !validTimestamp(t.completed_at)) fail('Shopping trip completion time does not match its status');
  const planIds = new Set();
  let previous = '', allBought = true;
  for (const w of t.weeks) {
    if (!exactKeys(w, tripWeekKeys) || !idPattern.test(w.plan_id || '') || !validDate(w.week_start) || !validDate(w.week_end) || w.week_end !== addDays(w.week_start, 6) || weekDay(w.week_start) !== 1 || !Number.isInteger(w.current_revision) || w.current_revision < 1 || !validGroups(w.groups)) fail('Invalid shopping trip week');
    if (w.week_start <= previous || planIds.has(w.plan_id)) fail('Shopping trip weeks must be unique and ordered by week start');
    previous = w.week_start; planIds.add(w.plan_id);
    if (!Array.isArray(w.cells) || w.cells.length !== w.groups.length || w.cells.some((cell, index) => cell?.group !== w.groups[index] || !validCell(cell, w))) fail('Shopping trip week cells do not match its groups');
    if (w.cells.some(cell => cell.state !== 'bought')) allBought = false;
  }
  if (t.status !== (allBought ? 'done' : 'open')) fail('Shopping trip status does not match its cells');
  for (const [field, list] of [['items', t.items], ['bought_items', t.bought_items]]) {
    const seen = new Set();
    for (const i of list) {
      validateTripItem(i, planIds, field);
      const key = `${i.ingredient_id}\u0000${i.unit}`;
      if (seen.has(key)) fail(`Duplicate shopping trip item in ${field}`); seen.add(key);
    }
  }
}
export function validateTripList(result) {
  if (!result || Object.keys(result).join(',') !== 'items' || !Array.isArray(result.items)) fail('Invalid shopping trip list');
  result.items.forEach(validateTrip);
  const ids = new Set(), weeks = new Set();
  for (const trip of result.items) {
    if (ids.has(trip.trip_id)) fail('Duplicate shopping trip');
    ids.add(trip.trip_id);
    for (const week of trip.weeks) for (const group of week.groups) { const pair = `${week.plan_id}|${group}`; if (weeks.has(pair)) fail('A week and group is on more than one shopping trip'); weeks.add(pair); }
  }
}
function validGroupCoverage(g) {
  return exactKeys(g, ['coverage', 'trip_id', 'changed_since_shopping']) && ['not_covered', 'to_buy', 'bought'].includes(g.coverage)
    && (g.coverage === 'not_covered' ? g.trip_id === null : idPattern.test(g.trip_id || ''))
    && typeof g.changed_since_shopping === 'boolean' && (!g.changed_since_shopping || g.coverage === 'bought');
}
export function validateCoverage(result) {
  if (!result || Object.keys(result).join(',') !== 'items' || !Array.isArray(result.items)) fail('Invalid coverage response');
  const ids = new Set();
  for (const c of result.items) {
    if (!exactKeys(c, ['plan_id','week_start','week_end','groups']) || !idPattern.test(c.plan_id || '') || !validDate(c.week_start) || !validDate(c.week_end) || c.week_end !== addDays(c.week_start, 6) || !exactKeys(c.groups, SHELF_LIVES) || !SHELF_LIVES.every(g => validGroupCoverage(c.groups[g])) || ids.has(c.plan_id)) fail('Invalid week coverage');
    ids.add(c.plan_id);
  }
}
// Coverage for plans overlapping a range, derived from trips (the static publication has no coverage endpoint). Per (week, group).
export function deriveCoverage(planSummaries, trips, range) {
  const byPair = new Map();
  for (const trip of trips) for (const week of trip.weeks) for (const group of week.groups) byPair.set(`${week.plan_id}|${group}`, { trip, week });
  const groupCoverage = (planId, group) => {
    const hit = byPair.get(`${planId}|${group}`);
    const cell = hit?.week.cells.find(entry => entry.group === group);
    return { coverage: cell ? cell.state : 'not_covered', trip_id: hit ? hit.trip.trip_id : null, changed_since_shopping: cell ? cell.changed_since_shopping : false };
  };
  const items = planSummaries.filter(s => s.week_start <= range.to && s.week_end >= range.from).sort((a, b) => a.week_start.localeCompare(b.week_start)).map(s => ({
    plan_id: s.plan_id, week_start: s.week_start, week_end: s.week_end,
    groups: { long_lasting: groupCoverage(s.plan_id, 'long_lasting'), fresh: groupCoverage(s.plan_id, 'fresh') },
  }));
  return { items };
}
export const validTripId = value => typeof value === 'string' && idPattern.test(value);
