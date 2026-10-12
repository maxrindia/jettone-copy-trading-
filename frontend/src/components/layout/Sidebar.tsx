"use client";

import type { ReactNode } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  LayoutDashboard,
  Landmark,
  ReceiptText,
  Scale,
  Boxes,
  PanelLeftClose,
  PanelLeftOpen,
  type LucideIcon,
} from "lucide-react";
import clsx from "clsx";
import { useSidebarVisibility } from "@/components/layout/SidebarVisibility";

type NavItem = { name: string; href: string; icon: LucideIcon };
type NavSection = { label: string | null; items: NavItem[] };

const sections: NavSection[] = [
  {
    label: "Overview",
    items: [
      { name: "Dashboard", href: "/", icon: LayoutDashboard },
      { name: "Group Strategy", href: "/group-strategy", icon: Boxes },
    ],
  },
  {
    label: null,
    items: [
      { name: "Demat Accounts", href: "/demat", icon: Landmark },
    ],
  },
  {
    label: null,
    items: [
      { name: "Order Manager", href: "/orders", icon: ReceiptText },
      { name: "Reconciliation", href: "/reconciliation", icon: Scale },
    ],
  },
];

function isItemActive(pathname: string, href: string): boolean {
  return (
    pathname === href ||
    (href !== "/" && pathname.startsWith(`${href}/`)) ||
    (href === "/group-strategy" && pathname === "/master")
  );
}

// Label shown beside a rail icon on hover/focus. Absolutely positioned, so it
// never takes layout space; the parent must be `group relative`.
function RailTooltip({ children }: { children: ReactNode }) {
  return (
    <span
      role="tooltip"
      className="pointer-events-none absolute left-full ml-3 top-1/2 -translate-y-1/2 whitespace-nowrap rounded-lg bg-[#111111] px-2 py-1 text-[11px] font-medium text-white shadow-md opacity-0 translate-x-[-4px] transition-[opacity,transform] duration-150 group-hover:opacity-100 group-hover:translate-x-0 group-focus-visible:opacity-100 group-focus-visible:translate-x-0 motion-reduce:transition-none"
    >
      {children}
    </span>
  );
}

// Sidebar chrome (not a navigation item). Available on every page; only the
// initial collapsed/expanded state differs per route (see SidebarVisibility).
function ToggleButton({ collapsed, onClick }: { collapsed: boolean; onClick: () => void }) {
  const label = collapsed ? "Expand navigation" : "Collapse navigation";
  const Icon = collapsed ? PanelLeftOpen : PanelLeftClose;
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      aria-expanded={!collapsed}
      // The rail shows the shared tooltip; the expanded label row has no room
      // for one, so it keeps the native title.
      title={collapsed ? undefined : label}
      className={clsx(
        "group relative shrink-0 flex items-center justify-center rounded-lg hover:text-[#111111] hover:bg-[#F7F7F7] focus:outline-none focus-visible:ring-2 focus-visible:ring-[#111111]/20 transition-colors",
        // Icon only: no permanent box or border, just a subtle hover tint.
        // The rail keeps its 40x32 hit area, so nothing around it moves.
        collapsed ? "w-10 h-8 text-[#6B7280]" : "h-7 w-7 text-[#6B7280]",
      )}
    >
      <Icon className={collapsed ? "w-[18px] h-[18px]" : "w-4 h-4"} aria-hidden />
      {collapsed && <RailTooltip>{label}</RailTooltip>}
    </button>
  );
}

export default function Sidebar() {
  const pathname = usePathname();
  const { canToggle, isVisible, toggle } = useSidebarVisibility();
  const collapsed = canToggle && !isVisible;

  return (
    <aside
      className={clsx(
        "relative z-40 bg-white border-r border-[#E5E7EB] flex flex-col shrink-0 h-full sticky top-0",
        "transition-[width] duration-200 ease-out motion-reduce:transition-none",
        collapsed ? "w-[68px]" : "w-[220px]",
      )}
    >
      {/* Brand */}
      <Link
        href="/"
        aria-label="Jettone home"
        className="h-16 flex items-center justify-center px-4 border-b border-[#E5E7EB] shrink-0"
      >
        {collapsed ? (
          <span className="w-8 h-8 rounded-lg bg-[#111111] text-white flex items-center justify-center text-[13px] font-bold tracking-tight">
            J
          </span>
        ) : (
          <img
            src="/logo.png"
            alt="Jettone Logo"
            className="h-9 sm:h-10 w-auto max-w-[175px] object-contain"
          />
        )}
      </Link>

      {collapsed ? (
        // Icon rail: top-aligned and evenly spaced, so it never re-centres when
        // the viewport height changes. One flat list - no per-group wrappers or
        // divider elements - so every icon is the same distance from the next.
        <nav aria-label="Main" className="flex-1 flex flex-col items-center gap-1 px-2 py-3">
          <div className="mb-2 shrink-0">
            <ToggleButton collapsed onClick={toggle} />
          </div>
          {sections.flatMap((section) => section.items).map((item) => {
            const isActive = isItemActive(pathname, item.href);
            const Icon = item.icon;
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-label={item.name}
                aria-current={isActive ? "page" : undefined}
                className={clsx(
                  "group relative w-10 h-10 shrink-0 flex items-center justify-center rounded-lg transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-[#111111]/20",
                  isActive
                    ? "bg-[#111111] text-white shadow-sm"
                    : "text-[#9CA3AF] hover:text-[#111111] hover:bg-[#F3F4F6]",
                )}
              >
                <Icon className="w-[18px] h-[18px]" aria-hidden />
                <RailTooltip>{item.name}</RailTooltip>
              </Link>
            );
          })}
        </nav>
      ) : (
        /* Navigation - one flat list so every item gets the same gap,
           regardless of which section (labeled or not) it belongs to. */
        <nav aria-label="Main" className="flex-1 px-2.5 py-4 space-y-2.5 overflow-y-auto">
          {sections.flatMap((section, sIdx) => {
            const elements: ReactNode[] = [];
            if (section.label) {
              elements.push(
                <div
                  key={`label-${sIdx}`}
                  className={clsx(
                    "flex items-center justify-between pl-3 text-[10px] font-bold text-[#9CA3AF] uppercase tracking-[0.1em]",
                    canToggle ? "pr-1 -my-1" : "pr-3 pb-1",
                  )}
                >
                  <span>{section.label}</span>
                  {canToggle && sIdx === 0 && <ToggleButton collapsed={false} onClick={toggle} />}
                </div>,
              );
            }
            section.items.forEach((item) => {
              const isActive = isItemActive(pathname, item.href);
              const Icon = item.icon;
              elements.push(
                <Link
                  key={item.href}
                  href={item.href}
                  aria-current={isActive ? "page" : undefined}
                  className={clsx(
                    "relative flex items-center gap-2.5 px-3 py-2 rounded-lg text-[13px] transition-colors",
                    isActive
                      ? "bg-[#111111] text-white font-semibold shadow-sm"
                      : "text-[#4B5563] font-medium hover:text-[#111111] hover:bg-[#F3F4F6]",
                  )}
                >
                  <Icon
                    className={clsx(
                      "w-4 h-4 shrink-0",
                      isActive ? "text-white" : "text-[#9CA3AF]",
                    )}
                    aria-hidden
                  />
                  <span className="truncate">{item.name}</span>
                </Link>,
              );
            });
            return elements;
          })}
        </nav>
      )}
    </aside>
  );
}
