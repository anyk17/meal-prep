// Pure helpers for the swap dialog (no React, no network) so they can be unit tested.
import { groupLabel, swapImpactEntries } from '../tripModel.js';

export function normalizeText(value) {
  return String(value ?? '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().trim();
}

export function proteinOptions(variants) {
  return [...new Set(variants.map(item => item.protein).filter(Boolean))].sort();
}

// Search by name (accent-insensitive, every word must match) and optionally filter by protein.
export function filterVariants(variants, query = '', protein = null) {
  const words = normalizeText(query).split(/\s+/).filter(Boolean);
  return variants.filter(item => {
    if (protein && item.protein !== protein) return false;
    const haystack = normalizeText(`${item.name} ${item.protein ?? ''} ${item.carb ?? ''}`);
    return words.every(word => haystack.includes(word));
  });
}

const CARB_NAMES = { platano_maduro: 'Plátano maduro', platano_verde: 'Plátano verde' };
export function carbLabel(carb) {
  if (!carb) return 'No carb';
  const name = CARB_NAMES[carb] ?? carb.replace(/_/g, ' ');
  return name.charAt(0).toUpperCase() + name.slice(1);
}

// One entry per recipe, its carb variants with "No carb" first, in catalog order otherwise.
export function groupRecipes(variants) {
  const recipes = new Map();
  for (const item of variants) {
    if (!recipes.has(item.recipe_id)) recipes.set(item.recipe_id, { recipe_id: item.recipe_id, name: item.name, protein: item.protein, variants: [] });
    recipes.get(item.recipe_id).variants.push(item);
  }
  for (const recipe of recipes.values()) recipe.variants.sort((a, b) => (a.carb ? 1 : 0) - (b.carb ? 1 : 0));
  return [...recipes.values()];
}

// Keep the slot's current carb when the new recipe offers it, else start with no carb.
export function defaultVariant(recipe, currentCarb) {
  return recipe.variants.find(item => (item.carb ?? null) === (currentCarb ?? null)) ?? recipe.variants[0];
}

export function canSwapSlot(slot, today) {
  return !!slot && typeof slot.date === 'string' && typeof today === 'string' && slot.date >= today;
}

export function isLinked(slot) { return !!slot?.linked_assignment_id; }

export const SCOPE_LABELS = {
  pair: 'Both lunch and dinner (keeps them together)',
  single: 'Only this meal (unlinks them)',
};

const UNIT_STEP = { g: 1, ml: 1, units: 0.1 };
export function formatAmount(value, unit) {
  const step = UNIT_STEP[unit] ?? 0.1;
  const rounded = step === 1 ? Math.round(value) : Math.round(value * 10) / 10;
  return `${rounded} ${unit}`;
}
export function formatDifference(difference, unit) {
  const sign = difference > 0 ? '+' : difference < 0 ? '−' : '';
  return `${sign}${formatAmount(Math.abs(difference), unit)}`;
}

// Group non-zero grocery deltas by section, increases and decreases tagged, sorted by name.
export function groupGroceryDelta(delta) {
  const sections = new Map();
  for (const item of delta) {
    if (!item || Math.abs(item.difference) < 1e-9) continue;
    const key = item.section || 'other';
    if (!sections.has(key)) sections.set(key, []);
    sections.get(key).push({
      ...item,
      direction: item.difference > 0 ? 'more' : 'less',
      text: formatDifference(item.difference, item.unit),
    });
  }
  return [...sections.entries()].sort(([a], [b]) => a.localeCompare(b))
    .map(([section, items]) => ({ section, items: items.sort((a, b) => a.display.localeCompare(b.display)) }));
}

const groupSuffix = groups => (groups?.length === 1 ? ` (${groupLabel(groups[0])})` : '');
// One list's impact. `groups` (optional) names the long-lasting/fresh part of the list the swapped week is on.
export function tripImpactMessage(trip) {
  if (!trip) return null;
  const where = groupSuffix(trip.groups);
  if (trip.status === 'open') return { kind: 'open', text: `Shopping list '${trip.name}'${where} will update automatically.` };
  const extras = (trip.extra_needed ?? []).map(item => `${item.display} ${formatAmount(item.quantity, item.unit)}`);
  return {
    kind: 'done',
    text: extras.length
      ? `'${trip.name}'${where} is already done — you'll need: ${extras.join(', ')}.`
      : `'${trip.name}'${where} is already done — you won't need anything extra.`,
    extras,
  };
}

// Messages for every list the week is on (one per list; a list holding both groups is one message).
export function tripImpactMessages(impact) {
  return swapImpactEntries(impact).map(entry => tripImpactMessage(entry));
}

export function replacedSwapsWarning(proposal) {
  const count = proposal?.replaced_swaps;
  if (!Number.isInteger(count) || count < 1) return null;
  return `This will replace ${count} ${count === 1 ? 'meal' : 'meals'} you swapped.`;
}

export const SWAP_WARNING_TITLE = 'Heads-up: this week breaks some planning rules';

export function swapErrorMessage(error) {
  switch (error?.code) {
    case 'stale_revision': return 'This week changed since you opened it. It has been reloaded; pick the swap again.';
    case 'slot_not_editable': return 'That meal is in the past, so it can no longer be swapped.';
    case 'slot_not_found': return 'That meal is no longer in this plan. The week has been reloaded.';
    case 'variant_not_found': return 'That recipe is no longer in the catalog. Pick another one.';
    case 'forbidden':
    case 'read_only': return 'This device is not allowed to change meal plans.';
    default: return error?.message || 'The swap failed.';
  }
}
export const swapNeedsReload = error => ['stale_revision', 'slot_not_found', 'slot_not_editable'].includes(error?.code);
