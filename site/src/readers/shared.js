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
