import React from 'react';
import { X, Utensils } from 'lucide-react';

export function RecipeModal({ slot, onClose }) {
    if (!slot) return null;

    const { recipeName, protein, carb, meal, day, proteinQty, proteinUnit, carbQty, carbUnit, ingredients } = slot;

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm animate-in fade-in duration-200">
            <div className="bg-white dark:bg-slate-900 rounded-2xl shadow-xl w-full max-w-md overflow-hidden animate-in zoom-in-95 duration-200 relative max-h-[90vh] flex flex-col transition-colors duration-200">
                <button
                    onClick={onClose}
                    className="absolute top-4 right-4 p-2 rounded-full hover:bg-gray-100 dark:hover:bg-slate-800 transition-colors z-10 text-slate-500 dark:text-slate-400"
                >
                    <X size={20} />
                </button>

                <div className="p-6 overflow-y-auto flex-1">
                    <div className="flex items-center gap-2 mb-2 text-sm text-gray-500 dark:text-slate-400 uppercase tracking-wider font-semibold">
                        <span>{day}</span>
                        <span>•</span>
                        <span>{meal}</span>
                    </div>

                    <h2 className="text-2xl font-bold text-gray-900 dark:text-white mb-4 pr-8">
                        {recipeName}
                    </h2>

                    <div className="space-y-4">
                        <div className="flex items-center gap-3 p-3 bg-slate-50 dark:bg-slate-800 rounded-lg">
                            <div className="p-2 bg-white dark:bg-slate-700 rounded-md shadow-sm">
                                <Utensils size={20} className="text-slate-600 dark:text-slate-200" />
                            </div>
                            <div className="flex-1">
                                <span className="block text-xs text-gray-500 dark:text-slate-400 uppercase">Protein</span>
                                <span className="font-medium capitalize text-slate-900 dark:text-white">{protein}</span>
                            </div>
                            <span className="font-mono font-bold text-lg text-slate-900 dark:text-white">{proteinQty} {proteinUnit}</span>
                        </div>

                        {carb !== 'none' && carbQty !== null && carbQty !== undefined && (
                            <div className="flex items-center gap-3 p-3 bg-slate-50 dark:bg-slate-800 rounded-lg">
                                <div className="p-2 bg-white dark:bg-slate-700 rounded-md shadow-sm">
                                    <span className="text-lg">🌾</span>
                                </div>
                                <div className="flex-1">
                                    <span className="block text-xs text-gray-500 dark:text-slate-400 uppercase">Carb</span>
                                    <span className="font-medium capitalize text-slate-900 dark:text-white">{carb}</span>
                                </div>
                                <span className="font-mono font-bold text-lg text-slate-900 dark:text-white">{carbQty} {carbUnit}</span>
                            </div>
                        )}

                        <div className="mt-6 pt-6 border-t border-gray-100 dark:border-slate-800">
                            <h3 className="font-semibold text-gray-900 dark:text-white mb-3">Ingredients</h3>
                            <ul className="space-y-2">
                                {ingredients.map((ing, idx) => (
                                    <li key={idx} className="flex justify-between text-sm py-1">
                                        <span className="text-gray-700 dark:text-slate-300">{ing.display}</span>
                                        <span className="font-mono text-gray-900 dark:text-slate-200 font-medium">
                                            {ing.qty} {ing.unit}
                                        </span>
                                    </li>
                                ))}
                            </ul>
                        </div>
                    </div>
                </div>

                <div className="bg-gray-50 dark:bg-slate-800/50 px-6 py-4 flex justify-end border-t border-gray-100 dark:border-slate-800">
                    <button
                        onClick={onClose}
                        className="px-4 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded-lg font-medium text-sm hover:bg-slate-800 dark:hover:bg-slate-600 transition-colors"
                    >
                        Close
                    </button>
                </div>
            </div>
        </div>
    );
}
