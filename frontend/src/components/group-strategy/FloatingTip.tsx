"use client";

import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { placeTooltip } from "@/lib/pnl-chart";

/**
 * A hover tooltip that keeps itself fully visible. It anchors to the element
 * marked `data-hover-anchor` inside its positioned parent (the hovered chart
 * point or bar), measures itself, and picks the safest spot with
 * `placeTooltip`, staying inside both the strategy card and the viewport.
 * Position is only a display matter; the content is whatever is passed in.
 */
export default function FloatingTip({ children, className }: { children: ReactNode; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);

  // Runs after every render (cheap) so a changed hover target re-places the
  // tip before paint; state is only set when the spot actually changes.
  useLayoutEffect(() => {
    const tip = ref.current;
    const parent = tip?.offsetParent as HTMLElement | null;
    const anchor = parent?.querySelector<HTMLElement>("[data-hover-anchor]");
    if (!tip || !parent || !anchor) return;
    const pr = parent.getBoundingClientRect();
    const ar = anchor.getBoundingClientRect();
    const card = parent.closest<HTMLElement>("[data-strategy-card]")?.getBoundingClientRect() ?? pr;
    // Available area: the card, limited to what the viewport shows (viewport coords).
    const vw = document.documentElement.clientWidth || window.innerWidth;
    const vh = window.innerHeight;
    const b = {
      left: Math.max(card.left, 0) - pr.left,
      top: Math.max(card.top, 0) - pr.top,
      right: Math.min(card.right, vw) - pr.left,
      bottom: Math.min(card.bottom, vh) - pr.top,
    };
    const next = placeTooltip({
      anchor: { x: ar.left + ar.width / 2 - pr.left, top: ar.top - pr.top, bottom: ar.bottom - pr.top },
      tip: { w: tip.offsetWidth, h: tip.offsetHeight },
      bounds: b,
    });
    setPos((cur) =>
      cur && Math.abs(cur.left - next.left) < 0.5 && Math.abs(cur.top - next.top) < 0.5 ? cur : { left: next.left, top: next.top },
    );
  });

  return (
    <div
      ref={ref}
      role="tooltip"
      className={className}
      style={{ position: "absolute", left: pos?.left ?? 0, top: pos?.top ?? 0, visibility: pos ? "visible" : "hidden", zIndex: 30, pointerEvents: "none" }}
    >
      {children}
    </div>
  );
}
