"use client";

import { createContext, ReactNode, useContext, useState, useSyncExternalStore } from "react";
import { usePathname } from "next/navigation";

// Pages whose sidebar STARTS collapsed to its icon rail so the content can use
// the full width. Every other page starts expanded. This only sets the initial
// state - the toggle itself is available on every page.
const COLLAPSED_BY_DEFAULT = new Set(["/orders"]);

// Below Tailwind's `md` breakpoint every page starts on the icon rail: the 220px
// sidebar would leave a phone-width viewport almost no room for the page.
const NARROW_QUERY = "(max-width: 767px)";
function subscribeNarrow(onChange: () => void) {
  const mq = window.matchMedia(NARROW_QUERY);
  mq.addEventListener("change", onChange);
  return () => mq.removeEventListener("change", onChange);
}
const getNarrow = () => window.matchMedia(NARROW_QUERY).matches;
const getNarrowOnServer = () => false;

type SidebarVisibility = {
  canToggle: boolean;
  isVisible: boolean; // true = expanded
  toggle: () => void;
};

const SidebarVisibilityContext = createContext<SidebarVisibility>({
  canToggle: false,
  isVisible: true,
  toggle: () => {},
});

export function SidebarVisibilityProvider({ children }: { children: ReactNode }) {
  const pathname = usePathname();

  // A manual choice belongs to the visit it was made on. It is dropped the
  // moment the pathname changes - during render rather than in an effect, so
  // there is never a frame with the previous page's state - which means every
  // visit to a page starts from that page's own default again.
  const [choice, setChoice] = useState<{ path: string; visible: boolean } | null>(null);
  if (choice !== null && choice.path !== pathname) setChoice(null);

  const narrow = useSyncExternalStore(subscribeNarrow, getNarrow, getNarrowOnServer);
  const defaultVisible = !narrow && !COLLAPSED_BY_DEFAULT.has(pathname);
  const isVisible = choice !== null && choice.path === pathname ? choice.visible : defaultVisible;

  const value: SidebarVisibility = {
    canToggle: true,
    isVisible,
    toggle: () => setChoice({ path: pathname, visible: !isVisible }),
  };

  return <SidebarVisibilityContext.Provider value={value}>{children}</SidebarVisibilityContext.Provider>;
}

export function useSidebarVisibility(): SidebarVisibility {
  return useContext(SidebarVisibilityContext);
}
