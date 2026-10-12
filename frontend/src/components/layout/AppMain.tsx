"use client";

import type { CSSProperties, ReactNode } from "react";
import { useSidebarVisibility } from "@/components/layout/SidebarVisibility";
import { SIDEBAR_EXTRA_PX } from "@/components/layout/sidebar-metrics";

/**
 * The page area beside the sidebar. Pages cap their content width
 * (`max-w-page` + `mx-auto`) and centre it in this area. When the sidebar
 * collapses the area gets SIDEBAR_EXTRA_PX wider; without help a capped page
 * would stay the same width and merely re-centre - sliding left, with wider
 * side gaps. Publishing the freed space as `--sidebar-extra` lets every
 * `max-w-page` cap grow by exactly that amount, so content uses the new space
 * and keeps the same margins to the sidebar and the right edge in both states.
 */
export default function AppMain({ children }: { children: ReactNode }) {
  const { canToggle, isVisible } = useSidebarVisibility();
  const collapsed = canToggle && !isVisible;

  return (
    <main
      style={{ "--sidebar-extra": `${collapsed ? SIDEBAR_EXTRA_PX : 0}px` } as CSSProperties}
      className="flex-1 min-h-0 overflow-y-auto [container-type:inline-size] px-4 pt-4 md:px-8 md:pt-8 pb-20 space-y-6 bg-[#F7F7F7]"
    >
      {children}
    </main>
  );
}
