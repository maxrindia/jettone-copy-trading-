"use client";

import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Check, ChevronDown } from "lucide-react";

export interface DropdownOption<V extends string> {
  value: V;
  label: string;
}

export interface DropdownGroup<V extends string> {
  label: string;
  options: readonly DropdownOption<V>[];
}

interface OptionDropdownProps<V extends string> {
  value: V;
  onChange: (value: V) => void;
  ariaLabel: string;
  /** Flat list of options. Use `groups` instead for headed sections. */
  options?: readonly DropdownOption<V>[];
  groups?: readonly DropdownGroup<V>[];
  /** Shown (in secondary grey) while `value` matches no option. */
  placeholder?: string;
  /** Trigger fills its container (form fields); otherwise it hugs its label. */
  fullWidth?: boolean;
  /** Which trigger edge the menu lines up with. */
  align?: "left" | "right";
  id?: string;
  /** Text colour of the selected label (default matches the Dashboard selector). */
  selectedTextClass?: string;
  /** Text colour of the unselected rows (default matches the Dashboard selector). */
  optionTextClass?: string;
}

/**
 * Single-choice dropdown with the same look as the Dashboard date-range
 * selector (DateRangeSelector.tsx): white trigger, #E5E7EB border, chevron, 10px
 * popover with a green check on the selected row. The popover is as wide as its
 * longest option (never narrower than the trigger), closes on outside click /
 * Escape / Tab, and is keyboard driven (Arrow keys, Home/End, Enter/Space).
 */
export default function OptionDropdown<V extends string>({
  value,
  onChange,
  ariaLabel,
  options,
  groups,
  placeholder,
  fullWidth = false,
  align = "right",
  id,
  selectedTextClass = "text-gray-800",
  optionTextClass = "text-gray-600",
}: OptionDropdownProps<V>) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const sections: readonly DropdownGroup<V>[] = groups ?? [{ label: "", options: options ?? [] }];
  const flat = sections.flatMap((s) => s.options);
  const selected = flat.find((o) => o.value === value);

  const optionButtons = () => Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="option"]') ?? []);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  // Move focus into the menu (selected row, else first) when it opens.
  useEffect(() => {
    if (!open) return;
    const buttons = optionButtons();
    (buttons.find((b) => b.getAttribute("aria-selected") === "true") ?? buttons[0])?.focus();
  }, [open]);

  const close = (returnFocus: boolean) => {
    setOpen(false);
    if (returnFocus) triggerRef.current?.focus();
  };

  const onMenuKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const buttons = optionButtons();
    const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
    if (e.key === "Escape") {
      // Closes only the menu: a host dialog's own window-level Escape handler must not also fire.
      e.preventDefault();
      e.stopPropagation();
      close(true);
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      buttons[Math.min(current + 1, buttons.length - 1)]?.focus();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      buttons[Math.max(current - 1, 0)]?.focus();
    } else if (e.key === "Home") {
      e.preventDefault();
      buttons[0]?.focus();
    } else if (e.key === "End") {
      e.preventDefault();
      buttons[buttons.length - 1]?.focus();
    } else if (e.key === "Tab") {
      setOpen(false);
    }
  };

  return (
    <div className={`relative ${fullWidth ? "w-full" : ""}`} ref={rootRef}>
      <button
        ref={triggerRef}
        id={id}
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown" && !open) {
            e.preventDefault();
            setOpen(true);
          } else if (e.key === "Escape" && open) {
            e.stopPropagation();
            setOpen(false);
          }
        }}
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        className={`group h-9 px-3 rounded-lg border border-gray-200 bg-white hover:bg-gray-50 hover:border-gray-300 transition-all flex items-center gap-2 shadow-[0_1px_2px_rgba(15,23,42,0.04)] focus:outline-none focus:ring-2 focus:ring-gray-400/20 cursor-pointer ${
          fullWidth ? "w-full justify-between" : ""
        }`}
      >
        <span
          className={`text-xs font-semibold tracking-tight whitespace-nowrap ${
            selected ? selectedTextClass : "text-[#6B7280]"
          }`}
        >
          {selected ? selected.label : placeholder ?? ""}
        </span>
        <ChevronDown
          className={`w-3.5 h-3.5 shrink-0 text-gray-400 transition-transform duration-200 ${
            open ? "rotate-180 text-gray-700" : "group-hover:text-gray-600"
          }`}
          aria-hidden
        />
      </button>

      {open && (
        <div
          ref={menuRef}
          role="listbox"
          aria-label={ariaLabel}
          onKeyDown={onMenuKeyDown}
          className={`absolute ${align === "right" ? "right-0" : "left-0"} top-full mt-1.5 w-max ${fullWidth ? "min-w-[12rem]" : "min-w-full"} max-w-[calc(100vw-2rem)] max-h-[min(28rem,65dvh)] overflow-y-auto rounded-[10px] bg-white border border-gray-100 shadow-xl shadow-gray-900/10 p-1.5 z-40 animate-in fade-in-50 zoom-in-95 duration-100`}
        >
          {sections.map((section, i) => (
            <div key={section.label || i} role={section.label ? "group" : undefined} aria-label={section.label || undefined} className={i > 0 ? "mt-1.5" : ""}>
              {section.label && (
                <div className="px-2.5 pt-1 pb-1 text-[11px] font-semibold text-[#6B7280]">{section.label}</div>
              )}
              <div className="space-y-0.5">
                {section.options.map((opt) => {
                  const isSelected = opt.value === value;
                  return (
                    <button
                      key={opt.value}
                      type="button"
                      role="option"
                      aria-selected={isSelected}
                      tabIndex={-1}
                      onClick={() => {
                        onChange(opt.value);
                        close(true);
                      }}
                      className={`w-full flex items-center justify-between gap-4 px-2.5 py-1.5 rounded-lg text-xs text-left whitespace-nowrap transition-colors cursor-pointer focus:outline-none focus-visible:bg-gray-100 ${
                        isSelected
                          ? "bg-gray-50 font-semibold text-[#111111]"
                          : `${optionTextClass} hover:bg-gray-50 hover:text-[#111111] font-medium`
                      }`}
                    >
                      <span>{opt.label}</span>
                      {isSelected && <Check className="w-3.5 h-3.5 text-green-600 shrink-0" aria-hidden />}
                    </button>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
