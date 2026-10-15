"use client";

import clsx from "clsx";
import { CalendarDays } from "lucide-react";
import FloatingTip from "@/components/group-strategy/FloatingTip";
import { formatInr, moneyToneClass } from "@/lib/money";
import { TOOLTIP_SURFACE_CLASS } from "@/components/ui/Tooltip";

/**
 * The ONE tooltip used by every Group Strategy P&L view (7-Day bars, Daily
 * line, Cumulative line): a calendar icon + date, then a single label / value
 * row. Nothing else - no partial/NFO/open-position explanation, no extra rows.
 * The chart's own title already says which view is showing, so the label is
 * "Daily P&L" for the day views and plain "P&L" for the cumulative one.
 *
 * Placement (beside / above / below the hovered point, always inside the card
 * and viewport) comes from FloatingTip, which anchors to the element marked
 * `data-hover-anchor`.
 */
export default function PnlTooltip({
  date,
  label,
  value,
}: {
  /** Already-formatted IST day, e.g. "Mon, 05 Oct". */
  date: string;
  label: string;
  /** Backend money string, or null when there is no verifiable value (shown as an em dash, never 0). */
  value: string | null;
}) {
  return (
    <FloatingTip className={clsx("w-max max-w-[220px] px-2.5 py-1.5", TOOLTIP_SURFACE_CLASS)}>
      <div className="flex items-center gap-1 text-[10px] font-semibold text-[#4B5563]">
        <CalendarDays className="h-3 w-3 shrink-0 text-gray-500" aria-hidden />
        <span>{date}</span>
      </div>
      <div className="mt-0.5 flex items-baseline justify-between gap-3">
        <span className="text-[10px] font-semibold text-[#6B7280]">{label}</span>
        <span className={clsx("font-mono text-[11px] font-bold tabular-nums", value === null ? "text-[#9CA3AF]" : moneyToneClass(value))}>
          {value === null ? "—" : formatInr(value)}
        </span>
      </div>
    </FloatingTip>
  );
}
