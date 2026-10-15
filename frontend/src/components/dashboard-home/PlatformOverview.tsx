"use client";

import type { DashboardPlatform } from "@/lib/dashboard";
import type { StaticIpCardState } from "@/lib/static-ip";
import { ActiveStrategiesCard, AvailableStaticIpCard, DematAccountsCard, StrategiesCard } from "./KpiCards";

/**
 * Row 1: current platform state (accounts, available static IP, strategy counts).
 * Deliberately independent of the dashboard's date-range selector - these
 * are "right now" facts about the platform, not trading-day analytics.
 *
 * The Static IP count comes from GET /system/static-ip. That request is owned
 * by the Dashboard page (started at page load, in parallel with the summary,
 * and kept across date-range changes) and handed in as `staticIp`: this row
 * is unmounted whenever the summary is loading, so a request owned by the row
 * itself would restart - and flash a skeleton - on every remount.
 */
export default function PlatformOverview({
  platform,
  staticIp,
  onRetryStaticIp,
}: {
  platform: DashboardPlatform;
  staticIp: StaticIpCardState;
  onRetryStaticIp?: () => void;
}) {
  return (
    <div className="grid grid-cols-1 [@container_(min-width:26rem)]:grid-cols-2 [@container_(min-width:56rem)]:grid-cols-4 gap-3">
      <DematAccountsCard platform={platform} />
      <AvailableStaticIpCard state={staticIp} onRetry={onRetryStaticIp} />
      <StrategiesCard platform={platform} />
      <ActiveStrategiesCard platform={platform} />
    </div>
  );
}
