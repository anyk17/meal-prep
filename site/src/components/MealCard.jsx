import React from 'react';
import { cn } from '../lib/utils';
import { Utensils, Fish, Beef, Drumstick, Egg, LeafyGreen } from 'lucide-react';

const PROTEIN_COLORS = {
    chicken: 'bg-yellow-100 text-yellow-800 border-yellow-200 dark:bg-yellow-900/30 dark:text-yellow-300 dark:border-yellow-700/50',
    beef: 'bg-red-100 text-red-800 border-red-200 dark:bg-red-900/30 dark:text-red-300 dark:border-red-700/50',
    pork: 'bg-pink-100 text-pink-800 border-pink-200 dark:bg-pink-900/30 dark:text-pink-300 dark:border-pink-700/50',
    fish: 'bg-blue-100 text-blue-800 border-blue-200 dark:bg-blue-900/30 dark:text-blue-300 dark:border-blue-700/50',
    egg: 'bg-orange-100 text-orange-800 border-orange-200 dark:bg-orange-900/30 dark:text-orange-300 dark:border-orange-700/50',
    vegetarian: 'bg-green-100 text-green-800 border-green-200 dark:bg-green-900/30 dark:text-green-300 dark:border-green-700/50',
    default: 'bg-gray-100 text-gray-800 border-gray-200 dark:bg-slate-800 dark:text-slate-300 dark:border-slate-700'
};

const PROTEIN_ICONS = {
    chicken: Drumstick,
    beef: Beef,
    pork: Beef, // Use Beef icon for pork for now
    fish: Fish,
    egg: Egg,
    vegetarian: LeafyGreen,
    default: Utensils
};

export function MealCard({ slot, onClick, highlighted = false, onSwap = null }) {
    const { recipeName, protein, carb, meal, proteinQty, proteinUnit, carbQty, carbUnit } = slot;

    const colorClass = PROTEIN_COLORS[protein] || PROTEIN_COLORS.default;
    const Icon = PROTEIN_ICONS[protein] || PROTEIN_ICONS.default;

    return (
        <div
            className={cn(
                "p-3 rounded-lg border shadow-sm hover:shadow-md transition-all cursor-pointer flex flex-col gap-2 relative overflow-hidden",
                colorClass,
                highlighted && "ring-2 ring-indigo-500 ring-offset-2 dark:ring-offset-slate-900"
            )}
            onClick={onClick}
        >
            <div className="flex items-center justify-between">
                <span className="text-xs font-semibold uppercase tracking-wider opacity-70">
                    {meal}
                </span>
                <Icon size={16} className="opacity-70" />
            </div>

            <h3 className="font-bold text-sm leading-tight line-clamp-2">
                {recipeName}
            </h3>

            <div className="mt-auto pt-2 text-xs space-y-1">
                <div className="flex items-center justify-between">
                    <span className="opacity-70">Protein:</span>
                    <span className="font-mono font-bold">{proteinQty} {proteinUnit}</span>
                </div>
                {carb !== 'none' && carbQty !== null && carbQty !== undefined && (
                    <div className="flex items-center justify-between">
                        <span className="opacity-70">Carb:</span>
                    <span className="font-mono font-bold">{carbQty} {carbUnit}</span>
                    </div>
                )}
            </div>
            {onSwap && (
                <button
                    type="button"
                    onClick={event => { event.stopPropagation(); onSwap(); }}
                    aria-label={`Swap ${recipeName}`}
                    className="min-h-11 mt-1 w-full rounded-md border border-current/30 bg-white/60 dark:bg-slate-900/40 text-xs font-semibold hover:bg-white/90 dark:hover:bg-slate-900/70"
                >
                    Swap
                </button>
            )}
        </div>
    );
}
