"use client";

import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { CalendarDays, ChevronLeft, ChevronRight, X } from "lucide-react";
import { monthGrid, monthLabel, parseIso, shiftMonth, todayIso, type YearMonth } from "@/lib/calendar";
import { formatExpiry } from "@/lib/static-ip";

const WEEKDAYS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];
const POPOVER_HEIGHT = 340; // enough for a 6-week month plus header and footer

/**
 * Small date-only picker (value is "" or YYYY-MM-DD, exactly like <input type="date">)
 * styled like the rest of the Jettone form controls. No third-party dependency; the
 * calendar math lives in lib/calendar.ts and never goes through local-time Dates.
 */
export default function DatePicker({
  value,
  onChange,
  ariaLabel,
  placeholder = "Select date",
  id,
}: {
  value: string;
  onChange: (value: string) => void;
  ariaLabel: string;
  placeholder?: string;
  id?: string;
}) {
  const [open, setOpen] = useState(false);
  const [openUp, setOpenUp] = useState(false);
  const [view, setView] = useState<YearMonth>({ year: 2026, month: 0 });
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const pendingFocus = useRef<"first" | "last" | null>(null);

  const selected = parseIso(value);
  const today = todayIso();

  const openPicker = () => {
    const base = parseIso(value) ?? parseIso(today)!;
    setView({ year: base.year, month: base.month });
    // Flip upward when there is no room below (the modal's action bar sits under the form).
    const rect = triggerRef.current?.getBoundingClientRect();
    setOpenUp(Boolean(rect && window.innerHeight - rect.bottom - 90 < POPOVER_HEIGHT && rect.top > POPOVER_HEIGHT));
    setOpen(true);
  };

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  // Focus the selected day (or today / first day) when the calendar opens.
  useEffect(() => {
    if (!open) return;
    const days = popoverRef.current?.querySelectorAll<HTMLButtonElement>("[data-day]");
    const target = popoverRef.current?.querySelector<HTMLButtonElement>(`[data-day="${value}"]`)
      ?? popoverRef.current?.querySelector<HTMLButtonElement>(`[data-day="${today}"]`)
      ?? days?.[0];
    target?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    if (!pendingFocus.current) return;
    const days = popoverRef.current?.querySelectorAll<HTMLButtonElement>("[data-day]");
    if (days && days.length > 0) (pendingFocus.current === "last" ? days[days.length - 1] : days[0]).focus();
    pendingFocus.current = null;
  }, [view]);

  const close = (returnFocus: boolean) => {
    setOpen(false);
    if (returnFocus) triggerRef.current?.focus();
  };

  const pick = (iso: string) => {
    onChange(iso);
    close(true);
  };

  const onPopoverKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      // Closes only the calendar, not a host dialog's window-level Escape handler.
      e.preventDefault();
      e.stopPropagation();
      close(true);
      return;
    }
    const step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[e.key as "ArrowLeft"];
    const active = document.activeElement as HTMLElement | null;
    if (step === undefined || !active?.dataset.day) return;
    e.preventDefault();
    const days = Array.from(popoverRef.current?.querySelectorAll<HTMLButtonElement>("[data-day]") ?? []);
    const next = days[days.indexOf(active as HTMLButtonElement) + step];
    if (next) next.focus();
    else {
      // Walked off the month: show the neighbouring month and land on its nearest day.
      pendingFocus.current = step < 0 ? "last" : "first";
      setView((v) => shiftMonth(v, step < 0 ? -1 : 1));
    }
  };

  const weeks = monthGrid(view);

  return (
    <div className="relative" ref={rootRef}>
      <div className="relative">
        <button
          ref={triggerRef}
          id={id}
          type="button"
          onClick={() => (open ? setOpen(false) : openPicker())}
          onKeyDown={(e) => {
            if (e.key === "Escape" && open) {
              e.stopPropagation();
              setOpen(false);
            }
          }}
          aria-label={ariaLabel}
          aria-haspopup="dialog"
          aria-expanded={open}
          className="w-full flex items-center gap-2 px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-left focus:outline-none focus:ring-2 focus:ring-[#111111] cursor-pointer"
        >
          <CalendarDays className="w-3.5 h-3.5 shrink-0 text-gray-500" aria-hidden />
          <span className={value ? "text-[#111111]" : "text-[#9CA3AF]"}>
            {value ? formatExpiry(value) || value : placeholder}
          </span>
        </button>
        {value && (
          <button
            type="button"
            onClick={() => onChange("")}
            aria-label="Clear date"
            className="absolute right-2 top-1/2 -translate-y-1/2 p-1 rounded-lg text-[#9CA3AF] hover:text-[#111111] hover:bg-gray-200 transition-colors"
          >
            <X className="w-3.5 h-3.5" aria-hidden />
          </button>
        )}
      </div>

      {open && (
        <div
          ref={popoverRef}
          role="dialog"
          aria-label={ariaLabel}
          onKeyDown={onPopoverKeyDown}
          className={`absolute left-0 z-40 w-[272px] max-w-[calc(100vw-2rem)] rounded-[10px] bg-white border border-gray-100 shadow-xl shadow-gray-900/10 p-3 animate-in fade-in-50 zoom-in-95 duration-100 ${
            openUp ? "bottom-full mb-1.5" : "top-full mt-1.5"
          }`}
        >
          <div className="flex items-center justify-between mb-2">
            <button
              type="button"
              onClick={() => setView((v) => shiftMonth(v, -1))}
              aria-label="Previous month"
              className="p-1.5 rounded-lg text-[#6B7280] hover:text-[#111111] hover:bg-gray-100 transition-colors"
            >
              <ChevronLeft className="w-4 h-4" aria-hidden />
            </button>
            <span className="text-xs font-semibold text-[#111111]" aria-live="polite">
              {monthLabel(view)}
            </span>
            <button
              type="button"
              onClick={() => setView((v) => shiftMonth(v, 1))}
              aria-label="Next month"
              className="p-1.5 rounded-lg text-[#6B7280] hover:text-[#111111] hover:bg-gray-100 transition-colors"
            >
              <ChevronRight className="w-4 h-4" aria-hidden />
            </button>
          </div>

          <div className="grid grid-cols-7 text-center text-[10px] font-semibold text-[#9CA3AF] mb-1">
            {WEEKDAYS.map((d) => (
              <span key={d} className="py-1">
                {d}
              </span>
            ))}
          </div>

          <div className="grid grid-cols-7 gap-y-0.5 text-center">
            {weeks.flat().map((iso, i) => {
              if (!iso) return <span key={`pad-${i}`} />;
              const isSelected = iso === value;
              const isToday = iso === today;
              return (
                <button
                  key={iso}
                  type="button"
                  data-day={iso}
                  tabIndex={-1}
                  onClick={() => pick(iso)}
                  aria-label={formatExpiry(iso)}
                  aria-pressed={isSelected}
                  className={`mx-auto h-8 w-8 rounded-lg text-xs transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-[#111111] ${
                    isSelected
                      ? "bg-[#E88900] text-white font-semibold hover:bg-[#D97700]"
                      : isToday
                      ? "border border-[#E88900] text-[#111111] font-semibold hover:bg-gray-100"
                      : "text-[#111111] hover:bg-gray-100"
                  }`}
                >
                  {Number(iso.slice(8))}
                </button>
              );
            })}
          </div>

          <div className="mt-2 pt-2 border-t border-[#F3F4F6] flex items-center justify-between text-xs font-semibold">
            <button type="button" onClick={() => pick(today)} className="px-2 py-1 rounded-lg text-[#E88900] hover:text-[#D97700] hover:bg-gray-50">
              Today
            </button>
            {selected && (
              <button
                type="button"
                onClick={() => {
                  onChange("");
                  close(true);
                }}
                className="px-2 py-1 rounded-lg text-[#6B7280] hover:text-[#111111] hover:bg-gray-50"
              >
                Clear
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
