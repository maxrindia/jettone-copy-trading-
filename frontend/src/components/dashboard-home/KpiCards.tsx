"use client";

import { ReactNode } from "react";
import { Activity, Boxes, CandlestickChart, CheckCircle2, Gauge, Landmark, Target, Wallet, Wifi } from "lucide-react";
import { formatInr, moneyToneClass } from "@/lib/money";
import { staticIpCaption, staticIpTitle, type StaticIpCounts } from "@/lib/static-ip";
import {
  combinedPnlUnavailableReason,
  edgeCaption,
  fillRateCaption,
  fillRateLabel,
  realizedPnlRangeCaption,
  realizedPnlUnavailableReason,
  unrealizedPnlRangeCaption,
  unrealizedPnlUnavailableReason,
  type CopyFillStats,
  type DashboardPlatform,
  type DashboardPnl,
  type DateRangeInfo,
  type ExecutionEdge,
} from "@/lib/dashboard";
import Tooltip from "@/components/ui/Tooltip";

/**
 * Clean, modern KPI card matching the reference design.
 * Features normal Title Case labels, circular icon badges,
 * high-contrast bold values, and subtle gray captions.
 */
interface KpiCardProps {
  icon?: ReactNode;
  iconColor?: string;
  halfCircleColor?: string | null;
  label: string;
  value: string;
  valueClassName?: string;
  // Base size/tracking for the primary value, independent of valueClassName
  // (which only ever carries color). Defaults to the original hero-number
  // treatment; the P&L cards pass a smaller size to match the Strategy
  // Performance table's body typography (see StrategyPerformanceTable.tsx).
  valueSizeClassName?: string;
  caption: ReactNode;
  captionWrap?: boolean;
  title?: string;
  onClick?: () => void;
}

function KpiCard({
  icon,
  iconColor = "text-gray-400 group-hover:text-gray-600",
  halfCircleColor,
  label,
  value,
  valueClassName,
  valueSizeClassName = "text-xl tracking-tight",
  caption,
  captionWrap = false,
  title,
  onClick,
}: KpiCardProps) {
  const Wrapper = onClick ? "button" : "div";
  return (
    <Tooltip content={title}>
    <Wrapper
      onClick={onClick}
      // The help text is on the card itself, so a non-clickable card must be focusable to reach it.
      tabIndex={!onClick && title ? 0 : undefined}
      className={`group relative p-4 rounded-[10px] bg-white border border-gray-100 shadow-[0_1px_2px_rgba(15,23,42,0.03)] hover:shadow-sm hover:border-gray-200 transition-all duration-150 text-left flex flex-col justify-between ${
        onClick ? "cursor-pointer" : ""
      }`}
    >
      {/* Optional top-right half-circle accent */}
      {halfCircleColor && (
        <span aria-hidden="true" className="pointer-events-none absolute inset-0 rounded-[10px] overflow-hidden">
          <span
            className={`absolute -top-7 -right-7 sm:-top-8 sm:-right-8 w-24 sm:w-28 h-24 sm:h-28 rounded-full -rotate-12 transition-transform duration-200 group-hover:scale-105 ${halfCircleColor}`}
          />
        </span>
      )}

      <div className="relative z-10 flex items-center justify-between gap-2 pr-1">
        <span className="text-xs font-medium text-gray-600 tracking-normal truncate">
          {label}
        </span>
        {icon && (
          <div className={`transition-colors shrink-0 ${iconColor}`}>
            {icon}
          </div>
        )}
      </div>

      <div className="relative z-10 mt-2.5">
        <p className={`font-bold font-sans leading-none ${valueSizeClassName} ${valueClassName ?? "text-[#111111]"}`}>
          {value}
        </p>
        <p className={`text-[11px] text-gray-400 font-normal mt-1 leading-snug ${captionWrap ? "" : "truncate"}`}>
          {caption}
        </p>
      </div>
    </Wrapper>
    </Tooltip>
  );
}

// ── Row 1: Platform Overview - never affected by the date range ────────────

export function DematAccountsCard({ platform }: { platform: DashboardPlatform }) {
  return (
    <KpiCard
      icon={<Landmark className="w-4 h-4" />}
      iconColor="text-[#111111]"
      halfCircleColor="bg-gray-100/80"
      label="Total Demat Accounts"
      value={String(platform.demat_accounts.total)}
      caption={`${platform.demat_accounts.connected} of ${platform.demat_accounts.total} connected`}
    />
  );
}

/**
 * Distinct, non-expired Static IP hosts across the demat accounts, as counted by
 * the engine (GET /system/static-ip). An IP with no recorded expiry is counted but
 * called out in the caption ("no expiry"), never reported as active or expired.
 * `counts` is null while the request is loading.
 */
export function AvailableStaticIpCard({ counts }: { counts: StaticIpCounts | null }) {
  const value = counts ? String(counts.available_count) : "—";

  return (
    <KpiCard
      icon={<Wifi className="w-4 h-4" />}
      iconColor="text-[#16A34A]"
      halfCircleColor="bg-green-100/70"
      label="Available Static IP"
      value={value}
      caption={staticIpCaption(counts)}
      title={staticIpTitle(counts)}
    />
  );
}

// Backward-compatibility alias
export const StaticIpCard = AvailableStaticIpCard;

export function StrategiesCard({ platform }: { platform: DashboardPlatform }) {
  return (
    <KpiCard
      icon={<Boxes className="w-4 h-4" />}
      iconColor="text-[#E88900]"
      halfCircleColor="bg-orange-100/70"
      label="Total Strategies"
      value={String(platform.strategies.total)}
      caption={`${platform.strategies.active} of ${platform.strategies.total} active`}
    />
  );
}

export function ActiveStrategiesCard({ platform }: { platform: DashboardPlatform }) {
  const isRunning = platform.strategies.active > 0;
  return (
    <KpiCard
      icon={<Activity className="w-4 h-4" />}
      iconColor="text-[#16A34A]"
      halfCircleColor="bg-green-100/70"
      label="Active Strategies"
      value={String(platform.strategies.active)}
      caption={isRunning ? "Live trading active" : "Currently idle"}
      title="Strategies enabled and linked to the live master."
    />
  );
}

// ── Row 2: Trading Performance - honors the selected date range ────────────

// Historical P&L is either verified or it is nothing - never a fabricated
// ₹0.00 standing in for "could not be calculated" (see
// backend/app/services/pnl/historical.py). This is deliberately different
// from this app's usual "show 0 instead of a dash" convention: that's for
// merely-empty data (no orders yet), while this is data that genuinely
// cannot be trusted - the two must never look the same.
//
// Presentation only: the large primary value reads "N/A" (not the heavier
// "Unavailable") with a single fixed caption, never a range-specific one
// ("for this range", "select Today", etc.) - the reason a given range is
// unavailable can vary, but the user-facing message stays generic. The
// backend's specific reason (pnl.*_unavailable_reason) is still surfaced via
// the `title` tooltip rather than dropped.
const PNL_UNAVAILABLE_CLASS = "text-gray-300";
const PNL_UNAVAILABLE_VALUE = "N/A";
const PNL_UNAVAILABLE_CAPTION = "P&L history isn't available yet";
// Matches the Strategy Performance table's first-column body typography
// (font-bold, no letter-spacing) at a size between that table's text-xs and
// this card's original text-xl hero size - see StrategyPerformanceTable.tsx.
const PNL_VALUE_SIZE_CLASS = "text-base";

export function TotalGrossPnlCard({ pnl, range }: { pnl: DashboardPnl; range: DateRangeInfo }) {
  const unavailable = combinedPnlUnavailableReason(pnl);
  return (
    <KpiCard
      icon={<Wallet className="w-4 h-4" />}
      iconColor="text-[#E88900]"
      halfCircleColor={null}
      label="Total Gross P&L"
      value={unavailable ? PNL_UNAVAILABLE_VALUE : formatInr(pnl.combined_total)}
      valueClassName={unavailable ? PNL_UNAVAILABLE_CLASS : moneyToneClass(pnl.combined_total)}
      valueSizeClassName={PNL_VALUE_SIZE_CLASS}
      title={unavailable ?? "Realized + Unrealized for every distinct account, for the selected period. Unrealized is only ever a right-now snapshot."}
      captionWrap
      caption={unavailable ? PNL_UNAVAILABLE_CAPTION : `Gross · Before charges · ${range.is_today ? "Today" : "selected period"}`}
    />
  );
}

export function RealizedPnlCard({ pnl, range }: { pnl: DashboardPnl; range: DateRangeInfo }) {
  const unavailable = realizedPnlUnavailableReason(pnl);
  return (
    <KpiCard
      icon={<CheckCircle2 className="w-4 h-4" />}
      iconColor="text-[#16A34A]"
      halfCircleColor={null}
      label="Realized P&L"
      value={unavailable ? PNL_UNAVAILABLE_VALUE : formatInr(pnl.combined_realized)}
      valueClassName={unavailable ? PNL_UNAVAILABLE_CLASS : moneyToneClass(pnl.combined_realized)}
      valueSizeClassName={PNL_VALUE_SIZE_CLASS}
      title={unavailable ?? undefined}
      caption={unavailable ? PNL_UNAVAILABLE_CAPTION : realizedPnlRangeCaption(range)}
    />
  );
}

export function UnrealizedPnlCard({ pnl, range }: { pnl: DashboardPnl; range: DateRangeInfo }) {
  const unavailable = unrealizedPnlUnavailableReason(pnl);
  return (
    <KpiCard
      icon={<CandlestickChart className="w-4 h-4" />}
      iconColor="text-[#2563EB]"
      halfCircleColor={null}
      label="Unrealized P&L"
      value={unavailable ? PNL_UNAVAILABLE_VALUE : formatInr(pnl.combined_unrealized)}
      valueClassName={unavailable ? PNL_UNAVAILABLE_CLASS : moneyToneClass(pnl.combined_unrealized)}
      valueSizeClassName={PNL_VALUE_SIZE_CLASS}
      title={unavailable ?? undefined}
      caption={unavailable ? PNL_UNAVAILABLE_CAPTION : unrealizedPnlRangeCaption(range)}
    />
  );
}

export function CopyFillRateCard({ fill }: { fill: CopyFillStats }) {
  return (
    <KpiCard
      icon={<Target className="w-4 h-4" />}
      iconColor="text-[#E88900]"
      halfCircleColor={null}
      label="Copy Fill Rate"
      value={fillRateLabel(fill)}
      title="Filled child quantity as a share of target child quantity from broker-confirmed fills."
      caption={fillRateCaption(fill)}
    />
  );
}

export function ExecutionImpactCard({ edge }: { edge: ExecutionEdge }) {
  return (
    <KpiCard
      icon={<Gauge className="w-4 h-4" />}
      iconColor="text-[#6B7280]"
      halfCircleColor={null}
      label="Execution Impact"
      value={formatInr(edge.total)}
      valueClassName={moneyToneClass(edge.total)}
      title="Quantity-weighted execution impact across copied fills."
      caption={edgeCaption(edge)}
    />
  );
}
