"use client";

import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ElementType,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { computeTooltipPosition, type TooltipPlacement, type TooltipSide } from "@/lib/tooltip";

/** One look for every informational tooltip (also used by the chart tooltips). */
export const TOOLTIP_SURFACE_CLASS =
  "rounded-lg border border-[#E5E7EB] bg-white shadow-[0_6px_20px_rgba(15,23,42,0.12)]";

const SHOW_DELAY_MS = 120;
const TOUCH_HIDE_MS = 2600;

/**
 * Shared informational tooltip: white card, soft border and shadow, small
 * arrow, subtle fade/scale. It replaces native `title` help text, whose black
 * box and timing the browser controls.
 *
 * - Rendered in a portal with fixed positioning, so cards and scroll
 *   containers never clip it; it flips above/below and stays inside the
 *   viewport.
 * - Opens on hover, keyboard focus anywhere inside, or touch; closes on
 *   leave, blur, Escape, scroll or after a short time on touch.
 * - `aria-describedby` points at the tooltip while it is open. Pass
 *   `focusable` for non-interactive content (a badge, a number) so keyboard
 *   users can reach the help; interactive children are already focusable.
 * - With no `content` it renders the children untouched.
 *
 * The wrapper uses `display: contents` by default, so it never changes the
 * layout of the element it explains; the tooltip anchors to its first child.
 */
export default function Tooltip({
  content,
  children,
  as: Wrapper = "span",
  className,
  side = "top",
  focusable = false,
}: {
  content: ReactNode;
  children: ReactNode;
  as?: ElementType;
  /** Extra classes for the wrapper (e.g. "block"); replaces the default `contents`. */
  className?: string;
  side?: TooltipSide;
  focusable?: boolean;
}) {
  const wrapperClass = className ?? (focusable ? "inline-block" : "contents");
  const hasContent = content !== null && content !== undefined && content !== false && content !== "";
  const id = useId();
  const wrapperRef = useRef<HTMLElement | null>(null);
  const tipRef = useRef<HTMLDivElement | null>(null);
  const showTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const hideTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [open, setOpen] = useState(false);
  const [placement, setPlacement] = useState<TooltipPlacement | null>(null);

  const clearTimers = () => {
    if (showTimer.current) clearTimeout(showTimer.current);
    if (hideTimer.current) clearTimeout(hideTimer.current);
    showTimer.current = null;
    hideTimer.current = null;
  };

  const hide = useCallback(() => {
    clearTimers();
    setOpen(false);
    setPlacement(null);
  }, []);

  const showSoon = () => {
    if (hideTimer.current) clearTimeout(hideTimer.current);
    if (open || showTimer.current) return;
    showTimer.current = setTimeout(() => {
      showTimer.current = null;
      setOpen(true);
    }, SHOW_DELAY_MS);
  };

  const showNow = () => {
    clearTimers();
    setOpen(true);
  };

  useEffect(() => clearTimers, []);

  // Measure and place after the tooltip is in the DOM, before paint.
  useLayoutEffect(() => {
    if (!open) return;
    const wrapper = wrapperRef.current;
    const tip = tipRef.current;
    if (!wrapper || !tip) return;
    // A `display: contents` wrapper has no box of its own: anchor to its child.
    const anchorEl = wrapperClass === "contents" ? ((wrapper.firstElementChild as HTMLElement | null) ?? wrapper) : wrapper;
    const r = anchorEl.getBoundingClientRect();
    setPlacement(
      computeTooltipPosition({
        anchor: { left: r.left, top: r.top, right: r.right, bottom: r.bottom },
        tip: { w: tip.offsetWidth, h: tip.offsetHeight },
        viewport: { w: document.documentElement.clientWidth || window.innerWidth, h: window.innerHeight },
        prefer: side,
      }),
    );
  }, [open, content, side, wrapperClass]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") hide();
    };
    const onMove = () => hide();
    window.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onMove, true);
    window.addEventListener("resize", onMove);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onMove, true);
      window.removeEventListener("resize", onMove);
    };
  }, [open, hide]);

  if (!hasContent) return <>{children}</>;

  return (
    <>
      <Wrapper
        ref={wrapperRef}
        className={wrapperClass}
        tabIndex={focusable ? 0 : undefined}
        aria-describedby={open ? id : undefined}
        onMouseEnter={showSoon}
        onMouseLeave={hide}
        onFocus={showNow}
        onBlur={hide}
        onTouchStart={() => {
          showNow();
          hideTimer.current = setTimeout(hide, TOUCH_HIDE_MS);
        }}
      >
        {children}
      </Wrapper>
      {open &&
        typeof document !== "undefined" &&
        createPortal(
          <div
            ref={tipRef}
            id={id}
            role="tooltip"
            data-side={placement?.side ?? side}
            style={{
              position: "fixed",
              left: placement?.left ?? 0,
              top: placement?.top ?? 0,
              // Hidden until measured, so it never flashes at 0,0.
              visibility: placement ? "visible" : "hidden",
            }}
            className={`pointer-events-none z-[10000] w-max max-w-[min(260px,calc(100vw-16px))] whitespace-pre-line px-2.5 py-1.5 text-[11px] font-medium leading-snug text-[#374151] ${TOOLTIP_SURFACE_CLASS} animate-[jt-tooltip-in_140ms_ease-out] motion-reduce:animate-none`}
          >
            {content}
            <span
              aria-hidden="true"
              style={{ left: (placement?.arrowLeft ?? 12) - 4 }}
              className={`absolute h-2 w-2 rotate-45 border-[#E5E7EB] bg-white ${
                (placement?.side ?? side) === "top"
                  ? "-bottom-1 border-b border-r"
                  : "-top-1 border-l border-t"
              }`}
            />
          </div>,
          document.body,
        )}
    </>
  );
}
