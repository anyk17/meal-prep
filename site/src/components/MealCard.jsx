import React, { useEffect, useRef, useState } from 'react';
import { cn } from '../lib/utils';
import { Utensils, Fish, Beef, Drumstick, Egg, LeafyGreen, MoreHorizontal, ArrowLeftRight, BookOpen } from 'lucide-react';

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

// Card overflow menu: Escape or a click outside closes it; arrow keys move between items.
function CardMenu({ label, actions }) {
    const [open, setOpen] = useState(false);
    const root = useRef(null);
    const trigger = useRef(null);

    useEffect(() => {
        if (!open) return undefined;
        root.current?.querySelector('[role="menuitem"]')?.focus();
        const close = event => { if (!root.current?.contains(event.target)) setOpen(false); };
        document.addEventListener('pointerdown', close);
        return () => document.removeEventListener('pointerdown', close);
    }, [open]);

    const onKeyDown = event => {
        const items = [...root.current.querySelectorAll('[role="menuitem"]')];
        const index = items.indexOf(document.activeElement);
        if (event.key === 'Escape') { setOpen(false); trigger.current?.focus(); }
        else if (event.key === 'ArrowDown') { event.preventDefault(); items[(index + 1) % items.length]?.focus(); }
        else if (event.key === 'ArrowUp') { event.preventDefault(); items[(index - 1 + items.length) % items.length]?.focus(); }
        else return;
        event.stopPropagation();
    };

    return (
        <div ref={root} className="relative -my-2 -mr-2" onClick={event => event.stopPropagation()} onKeyDown={onKeyDown}>
            <button
                ref={trigger}
                type="button"
                aria-label={label}
                aria-haspopup="menu"
                aria-expanded={open}
                onClick={() => setOpen(value => !value)}
                className="w-11 h-11 flex items-center justify-center rounded-md opacity-70 hover:opacity-100 hover:bg-black/5 dark:hover:bg-white/10 focus-visible:outline-2 focus-visible:outline-current"
            >
                <MoreHorizontal size={18} />
            </button>
            {open && (
                <div role="menu" className="absolute right-0 top-full z-30 mt-1 min-w-44 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 py-1 shadow-lg text-slate-700 dark:text-slate-200">
                    {actions.map(({ label: text, icon, onSelect }) => (
                        <button
                            key={text}
                            type="button"
                            role="menuitem"
                            onClick={() => { setOpen(false); onSelect(); }}
                            className="w-full min-h-11 px-3 flex items-center gap-2 text-sm text-left hover:bg-slate-100 dark:hover:bg-slate-800 focus:bg-slate-100 dark:focus:bg-slate-800 focus:outline-none"
                        >
                            {icon}{text}
                        </button>
                    ))}
                </div>
            )}
        </div>
    );
}

export function MealCard({ slot, onClick, highlighted = false, onSwap = null }) {
    const { recipeName, protein, carb, meal, proteinQty, proteinUnit, carbQty, carbUnit } = slot;

    const colorClass = PROTEIN_COLORS[protein] || PROTEIN_COLORS.default;
    const Icon = PROTEIN_ICONS[protein] || PROTEIN_ICONS.default;

    return (
        <div
            className={cn(
                "p-3 rounded-lg border shadow-sm hover:shadow-md transition-all cursor-pointer flex flex-col gap-2 relative",
                colorClass,
                highlighted && "ring-2 ring-indigo-500 ring-offset-2 dark:ring-offset-slate-900"
            )}
            onClick={onClick}
        >
            <div className="flex items-center justify-between">
                <span className="text-xs font-semibold uppercase tracking-wider opacity-70">
                    {meal}
                </span>
                <div className="flex items-center gap-1">
                    <Icon size={16} className="opacity-70" />
                    <CardMenu
                        label={`Actions for ${recipeName}`}
                        actions={[
                            ...(onSwap ? [{ label: 'Swap meal', icon: <ArrowLeftRight size={16} className="opacity-70" />, onSelect: onSwap }] : []),
                            { label: 'View recipe', icon: <BookOpen size={16} className="opacity-70" />, onSelect: () => onClick?.() },
                        ]}
                    />
                </div>
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
        </div>
    );
}
