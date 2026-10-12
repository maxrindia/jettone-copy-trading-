"use client";

import { useEngineData } from "@/lib/use-engine-data";
import type { DashboardPlatform } from "@/lib/dashboard";
import type { StaticIpCounts } from "@/lib/static-ip";
import { ActiveStrategiesCard, AvailableStaticIpCard, DematAccountsCard, StrategiesCard } from "./KpiCards";

interface StaticIpStatus extends StaticIpCounts {
  vps_ip: string;
  is_whitelisted: boolean;
  last_verified_at: string;
}

/**
 * Row 1: current platform state (accounts, available static IP, strategy counts).
 * Deliberately independent of the dashboard's date-range selector - these
 * are "right now" facts about the platform, not trading-day analytics.
 * Static IP is fetched from the existing GET /system/static-ip rather than
 * duplicated into /dashboard/summary.
 */
export default function PlatformOverview({ platform }: { platform: DashboardPlatform }) {
  const staticIp = useEngineData<StaticIpStatus>("/system/static-ip", 60000);

  return (
    <div className="grid grid-cols-1 [@container_(min-width:26rem)]:grid-cols-2 [@container_(min-width:56rem)]:grid-cols-4 gap-3">
      <DematAccountsCard platform={platform} />
      <AvailableStaticIpCard counts={staticIp.data ?? null} />
      <StrategiesCard platform={platform} />
      <ActiveStrategiesCard platform={platform} />
    </div>
  );
}
