/**
 * Sidebar widths, in px. They mirror the classes in Sidebar.tsx
 * (`w-[220px]` expanded / `w-[68px]` icon rail); sidebar-metrics.test.ts fails
 * if the two ever drift apart.
 *
 * AppMain publishes the difference as `--sidebar-extra`, so a page's width cap
 * (`max-w-page`) grows by exactly the space a collapsed sidebar frees up.
 */
export const SIDEBAR_EXPANDED_PX = 220;
export const SIDEBAR_COLLAPSED_PX = 68;
export const SIDEBAR_EXTRA_PX = SIDEBAR_EXPANDED_PX - SIDEBAR_COLLAPSED_PX;
