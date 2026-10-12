"use client";

import { useState, useRef, useEffect } from "react";
import { CalendarDays, ChevronDown, Check } from "lucide-react";
import { DATE_RANGE_OPTIONS, type DateRangeKey } from "@/lib/dashboard";

/**
 * The global trading-analytics date filter, placed near the Dashboard
 * heading. Deliberately NOT wired to Row 1 (Platform Overview) - see
 * PlatformOverview.tsx - only the date-sensitive sections (Row 2, Strategy
 * Performance, both charts) read this value.
 */
export default function DateRangeSelector({
  range,
  customFrom,
  customTo,
  onChangeRange,
  onChangeCustom,
}: {
  range: DateRangeKey;
  customFrom: string;
  customTo: string;
  onChangeRange: (range: DateRangeKey) => void;
  onChangeCustom: (from: string, to: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  const selectedOption =
    DATE_RANGE_OPTIONS.find((opt) => opt.key === range) ?? DATE_RANGE_OPTIONS[0];

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    }
    if (open) {
      document.addEventListener("mousedown", handleClickOutside);
    }
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [open]);

  return (
    <div className="flex flex-wrap items-center gap-2">
      <div className="relative" ref={dropdownRef}>
        <button
          type="button"
          onClick={() => setOpen((prev) => !prev)}
          aria-label="Trading analytics date range"
          aria-expanded={open}
          className="group h-9 px-3 rounded-lg border border-gray-200 bg-white hover:bg-gray-50 hover:border-gray-300 transition-all flex items-center gap-2 shadow-[0_1px_2px_rgba(15,23,42,0.04)] focus:outline-none focus:ring-2 focus:ring-gray-400/20 cursor-pointer"
        >
          <CalendarDays className="w-4 h-4 text-gray-500 group-hover:text-gray-700 transition-colors" aria-hidden />
          <span className="text-xs font-semibold text-gray-800 tracking-tight">
            {selectedOption.label}
          </span>
          <ChevronDown
            className={`w-3.5 h-3.5 text-gray-400 transition-transform duration-200 ${
              open ? "rotate-180 text-gray-700" : "group-hover:text-gray-600"
            }`}
            aria-hidden
          />
        </button>

        {open && (
          <div className="absolute right-0 top-full mt-1.5 w-48 rounded-[10px] bg-white border border-gray-100 shadow-xl shadow-gray-900/10 p-1.5 z-40 animate-in fade-in-50 zoom-in-95 duration-100">
            <div className="space-y-0.5">
              {DATE_RANGE_OPTIONS.map((opt) => {
                const isSelected = opt.key === range;
                return (
                  <button
                    key={opt.key}
                    type="button"
                    onClick={() => {
                      onChangeRange(opt.key);
                      setOpen(false);
                    }}
                    className={`w-full flex items-center justify-between px-2.5 py-1.5 rounded-lg text-xs transition-colors cursor-pointer ${
                      isSelected
                        ? "bg-gray-50 font-semibold text-[#111111]"
                        : "text-gray-600 hover:bg-gray-50 hover:text-[#111111] font-medium"
                    }`}
                  >
                    <span>{opt.label}</span>
                    {isSelected && (
                      <Check className="w-3.5 h-3.5 text-green-600 shrink-0" aria-hidden />
                    )}
                  </button>
                );
              })}
            </div>
          </div>
        )}
      </div>

      {range === "custom" && (
        <div className="flex items-center gap-1.5 text-xs">
          <input
            type="date"
            value={customFrom}
            max={customTo || undefined}
            onChange={(e) => onChangeCustom(e.target.value, customTo)}
            aria-label="Custom range start date"
            className="h-9 px-2.5 rounded-lg border border-gray-200 bg-white text-[#111111] text-xs font-medium focus:outline-none focus:border-gray-400 shadow-sm"
          />
          <span className="text-gray-400 font-medium text-xs">to</span>
          <input
            type="date"
            value={customTo}
            min={customFrom || undefined}
            onChange={(e) => onChangeCustom(customFrom, e.target.value)}
            aria-label="Custom range end date"
            className="h-9 px-2.5 rounded-lg border border-gray-200 bg-white text-[#111111] text-xs font-medium focus:outline-none focus:border-gray-400 shadow-sm"
          />
        </div>
      )}
    </div>
  );
}
