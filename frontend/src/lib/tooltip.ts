/**
 * Placement for the shared Tooltip, kept free of the DOM so it can be tested.
 * All numbers are viewport coordinates (position: fixed).
 */

export interface Rect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export type TooltipSide = "top" | "bottom";

export interface TooltipPlacementInput {
  anchor: Rect;
  tip: { w: number; h: number };
  viewport: { w: number; h: number };
  /** Preferred side; flipped when it does not fit and the other side does. */
  prefer?: TooltipSide;
  /** Space between the anchor and the tooltip (room for the arrow). */
  gap?: number;
  /** Minimum distance kept from every viewport edge. */
  margin?: number;
}

export interface TooltipPlacement {
  left: number;
  top: number;
  side: TooltipSide;
  /** Arrow centre, measured from the tooltip's own left edge. */
  arrowLeft: number;
}

const ARROW_INSET = 10;

export function computeTooltipPosition({
  anchor,
  tip,
  viewport,
  prefer = "top",
  gap = 8,
  margin = 8,
}: TooltipPlacementInput): TooltipPlacement {
  const spaceAbove = anchor.top - margin;
  const spaceBelow = viewport.h - anchor.bottom - margin;
  const need = tip.h + gap;

  let side: TooltipSide = prefer;
  if (prefer === "top" && spaceAbove < need && spaceBelow >= need) side = "bottom";
  else if (prefer === "bottom" && spaceBelow < need && spaceAbove >= need) side = "top";
  // Neither fits: take the roomier side so the tooltip is as visible as possible.
  else if (spaceAbove < need && spaceBelow < need) side = spaceBelow > spaceAbove ? "bottom" : "top";

  const anchorCentre = (anchor.left + anchor.right) / 2;
  const maxLeft = Math.max(margin, viewport.w - tip.w - margin);
  const left = Math.min(Math.max(anchorCentre - tip.w / 2, margin), maxLeft);

  const rawTop = side === "top" ? anchor.top - gap - tip.h : anchor.bottom + gap;
  const maxTop = Math.max(margin, viewport.h - tip.h - margin);
  const top = Math.min(Math.max(rawTop, margin), maxTop);

  const arrowLeft = Math.min(Math.max(anchorCentre - left, ARROW_INSET), Math.max(ARROW_INSET, tip.w - ARROW_INSET));
  return { left, top, side, arrowLeft };
}
