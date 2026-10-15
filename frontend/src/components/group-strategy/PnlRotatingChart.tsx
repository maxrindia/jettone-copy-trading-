"use client";

import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import clsx from "clsx";
import { Info } from "lucide-react";
import type { StrategyDailyPnl } from "@/lib/dashboard";
import PnlTooltip from "@/components/group-strategy/PnlTooltip";
import {
  CHART_PAD_BOT,
  CHART_PAD_TOP,
  CHART_PAD_X,
  CHART_VB_H,
  CHART_VB_W,
  PNL_CHART_MODES,
  buildPnlChartSeries,
  layoutPnlChart,
  nearestPointIndex,
  nextChartIndex,
  pnlChartNote,
  pnlChartRuns,
  smoothPath,
  type PnlChartLayout,
  type PnlChartPoint,
  type PnlChartSeries,
} from "@/lib/pnl-chart";
import HelpTooltip from "@/components/ui/Tooltip";

/**
 * The Group Strategy card's P&L frame: ONE fixed-size frame whose content
 * rotates, every ~3 seconds, through three views of the already-loaded daily
 * data - 7-Day P&L (the bar view, built by StrategyPnlSnapshot and passed in
 * as `barsLayer`), Daily P&L and Cumulative P&L (line charts). Only the active
 * view is visible, and Today / 7 days belong to the bar view alone.
 *
 * Line look: the original Group Strategy chart (same 380x120 drawing box, 2px
 * line, green above zero / red below with a soft fill, three faint grid
 * lines, small white tooltip). Missing data is never drawn as zero: a day
 * with no verifiable value is a gap in the line (dashed across it in the
 * cumulative view). There are no permanent point markers: the line is clean,
 * and only the hovered point shows a marker (with the tooltip, which says if
 * a day is partial).
 *
 * Motion is purely visual and uses only the points already on screen:
 *  - switching views slides the old chart out to the left while the next one
 *    enters from the right;
 *  - when a view has more days than fit, its viewport starts a few days
 *    behind and pans left to the latest day, so the newest data enters from
 *    the right and older data leaves to the left. Nothing is fetched,
 *    generated or interpolated.
 * Hovering pauses the rotation and shows the tooltip. For
 * prefers-reduced-motion the views swap without sliding or panning.
 */

const ROTATE_MS = 3000;
const SLIDE_MS = 700;
const PAN_MS = 2200;
const GREEN = "#16A34A";
const RED = "#EF4444";
const NEUTRAL = "#9CA3AF";

const toneColor = (v: string | null): string => {
  const n = v === null ? 0 : Number(v);
  return n > 0 ? GREEN : n < 0 ? RED : NEUTRAL;
};

function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(media.matches);
    update();
    media.addEventListener?.("change", update);
    return () => media.removeEventListener?.("change", update);
  }, []);
  return reduced;
}

function formatDay(iso: string): string {
  // Already an IST trading day; format in UTC so the viewer's timezone can
  // never move it to a neighbouring day.
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-IN", {
    weekday: "short",
    day: "2-digit",
    month: "short",
    timeZone: "UTC",
  });
}

interface ChartData {
  series: PnlChartSeries;
  layout: PnlChartLayout;
  runs: ReturnType<typeof pnlChartRuns>;
  note: string | null;
}

/** One view's line, drawn in a box `layout.totalW` wide (wider than the card when it pans). */
function ChartBody({
  data,
  uid,
  hover,
  bodyRef,
}: {
  data: ChartData;
  uid: string;
  hover: number | null;
  /** Receives the element that the entrance pan animates. */
  bodyRef: (el: HTMLDivElement | null) => void;
}) {
  const { series, layout, runs } = data;
  const { xs, ys, zeroY } = layout;
  const clipAbove = `${uid}${series.mode}a`;
  const clipBelow = `${uid}${series.mode}b`;
  const greenFill = `${uid}${series.mode}g`;
  const redFill = `${uid}${series.mode}r`;

  const at = (idx: number[]) => idx.map((i) => ({ x: xs[i], y: ys[i] as number }));
  const lines = runs.solid.filter((r) => r.length > 1).map((r) => ({ d: smoothPath(at(r)), first: xs[r[0]], last: xs[r[r.length - 1]] }));
  // A verified day with no neighbour to join to (e.g. the only day, or one
  // between unverifiable days) has no line to be part of. It is drawn as a
  // tiny flat tick of the same line, not a dot, so a real value is never invisible.
  const ticks = runs.solid
    .filter((r) => r.length === 1)
    .map((r) => `M${(xs[r[0]] - 4).toFixed(2)} ${(ys[r[0]] as number).toFixed(2)} L${(xs[r[0]] + 4).toFixed(2)} ${(ys[r[0]] as number).toFixed(2)}`)
    .join(" ");
  const dashed = runs.dashed.map(([a, b]) => `M${xs[a].toFixed(2)} ${(ys[a] as number).toFixed(2)} L${xs[b].toFixed(2)} ${(ys[b] as number).toFixed(2)}`).join(" ");

  const pctX = (i: number) => (xs[i] / layout.totalW) * 100;
  const pctY = (i: number) => ((ys[i] as number) / CHART_VB_H) * 100;
  // The ONLY marker: the hovered point. Nothing is marked in the normal state.
  const marked = hover !== null && ys[hover] !== null ? hover : -1;

  return (
    <div
      ref={bodyRef}
      className="absolute bottom-0 left-0 top-0"
      style={{ width: `${(layout.totalW / CHART_VB_W) * 100}%`, transform: `translateX(-${layout.endPct}%)` }}
      data-chart-body={series.mode}
    >
      <svg className="absolute inset-0 h-full w-full" viewBox={`0 0 ${layout.totalW} ${CHART_VB_H}`} preserveAspectRatio="none" aria-hidden>
        <defs>
          <clipPath id={clipAbove}><rect x="0" y="0" width={layout.totalW} height={Math.max(0, zeroY)} /></clipPath>
          <clipPath id={clipBelow}><rect x="0" y={zeroY} width={layout.totalW} height={Math.max(0, CHART_VB_H - zeroY)} /></clipPath>
          <linearGradient id={greenFill} gradientUnits="userSpaceOnUse" x1="0" y1={Math.min(CHART_PAD_TOP, zeroY - 1)} x2="0" y2={zeroY}>
            <stop offset="0%" stopColor="#16A34A" stopOpacity="0.28" />
            <stop offset="60%" stopColor="#16A34A" stopOpacity="0.10" />
            <stop offset="100%" stopColor="#16A34A" stopOpacity="0.02" />
          </linearGradient>
          <linearGradient id={redFill} gradientUnits="userSpaceOnUse" x1="0" y1={zeroY} x2="0" y2={Math.max(CHART_VB_H - CHART_PAD_BOT, zeroY + 1)}>
            <stop offset="0%" stopColor="#EF4444" stopOpacity="0.02" />
            <stop offset="40%" stopColor="#EF4444" stopOpacity="0.10" />
            <stop offset="100%" stopColor="#EF4444" stopOpacity="0.26" />
          </linearGradient>
        </defs>

        {layout.allZero ? (
          // Every verified value is exactly 0: one neutral line, no fills.
          <g fill="none" stroke={NEUTRAL} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" opacity="0.8">
            {lines.map((l, i) => <path key={i} d={l.d} vectorEffect="non-scaling-stroke" />)}
            {ticks && <path d={ticks} vectorEffect="non-scaling-stroke" />}
          </g>
        ) : (
          <>
            {/* Soft fills against the zero line, green above and red below. */}
            {lines.map((l, i) => {
              const area = `${l.d} L${l.last.toFixed(2)} ${zeroY.toFixed(2)} L${l.first.toFixed(2)} ${zeroY.toFixed(2)} Z`;
              return (
                <g key={`a${i}`} stroke="none">
                  <path d={area} fill={`url(#${greenFill})`} clipPath={`url(#${clipAbove})`} />
                  <path d={area} fill={`url(#${redFill})`} clipPath={`url(#${clipBelow})`} />
                </g>
              );
            })}
            {/* The line, drawn twice and clipped to either side of zero. */}
            {([[clipAbove, GREEN], [clipBelow, RED]] as const).map(([clip, color]) => (
              <g key={clip} clipPath={`url(#${clip})`} fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                {lines.map((l, i) => <path key={i} d={l.d} vectorEffect="non-scaling-stroke" />)}
                {ticks && <path d={ticks} vectorEffect="non-scaling-stroke" />}
                {dashed && <path d={dashed} strokeDasharray="3 4" opacity="0.9" vectorEffect="non-scaling-stroke" />}
              </g>
            ))}
          </>
        )}
      </svg>

      {/* No permanent point markers. While the pointer is over the chart, the
          hovered point alone gets one (HTML, so it stays round while the SVG
          stretches); it goes away when the pointer leaves. The invisible
          anchor tells the tooltip where the hovered point is, whether or not
          that day has a value to mark. */}
      {hover !== null && (
        <span
          data-hover-anchor
          className="pointer-events-none absolute h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2"
          style={{ left: `${pctX(hover)}%`, top: ys[hover] !== null ? `${pctY(hover)}%` : "50%" }}
        >
          {marked >= 0 && (
            <span
              data-hover-marker
              className="block h-2.5 w-2.5 rounded-full border-2 bg-white shadow-[0_1px_3px_rgba(15,23,42,0.18)]"
              style={{ borderColor: toneColor(series.points[marked].value) }}
            />
          )}
        </span>
      )}
    </div>
  );
}

function Tooltip({ data, index }: { data: ChartData; index: number }) {
  const { series } = data;
  const p: PnlChartPoint = series.points[index];
  // Same compact tooltip as the 7-Day bars. The chart title already names the
  // view, so the cumulative view just says "P&L".
  return series.mode === "cumulative" ? (
    <PnlTooltip date={formatDay(p.date)} label="P&L" value={p.cumulative} />
  ) : (
    <PnlTooltip date={formatDay(p.date)} label="Daily P&L" value={p.daily} />
  );
}

export default function PnlRotatingChart({
  daily,
  startIso,
  barsLayer,
  barsTooltip,
}: {
  daily: StrategyDailyPnl[];
  /** The strategy's creation day in IST (YYYY-MM-DD). */
  startIso: string;
  /** The 7-Day P&L view (Today / 7 days / bars / caption), first in the rotation. */
  barsLayer: ReactNode;
  /** The bars' hover tooltip; rendered above the clipped frame, only in the 7-Day view. */
  barsTooltip: ReactNode;
}) {
  const uid = useId().replace(/[^a-zA-Z0-9]/g, "");
  const reduced = usePrefersReducedMotion();

  const charts: ChartData[] = useMemo(
    () =>
      PNL_CHART_MODES.map((mode) => {
        const series = buildPnlChartSeries(daily, startIso, mode);
        return { series, layout: layoutPnlChart(series), runs: pnlChartRuns(series), note: pnlChartNote(series) };
      }),
    [daily, startIso],
  );
  const chartsRef = useRef(charts);
  chartsRef.current = charts;

  // View 0 is the 7-Day bars; views 1.. are the line charts (charts[i - 1]).
  // `prev` is the view sliding out, which stays visible until it is gone.
  const [view, setView] = useState<{ active: number; prev: number | null }>({ active: 0, prev: null });
  const active = view.active;
  const nViews = charts.length + 1;
  const setActive = (next: number | ((a: number) => number)) =>
    setView((v) => {
      const to = typeof next === "function" ? next(v.active) : next;
      return to === v.active ? v : { active: to, prev: v.active };
    });
  const [paused, setPaused] = useState(false);
  const [hover, setHover] = useState<number | null>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const bodyRefs = useRef<(HTMLDivElement | null)[]>([]);
  const layerRefs = useRef<(HTMLDivElement | null)[]>([]);
  const prevActiveRef = useRef(0);
  const panRef = useRef<Animation | null>(null);

  // Rotation: one timer, one state update per view change. Paused while the
  // pointer is over the chart (so the tooltip stays put).
  useEffect(() => {
    if (paused) return;
    const t = setTimeout(() => setActive((a) => nextChartIndex(a, nViews)), ROTATE_MS);
    return () => clearTimeout(t);
  }, [active, paused, nViews]);

  // Once the slide-out has finished, the old view is hidden for good.
  useEffect(() => {
    if (view.prev === null) return;
    const t = setTimeout(() => setView((v) => (v === view ? { ...v, prev: null } : v)), SLIDE_MS + 100);
    return () => clearTimeout(t);
  }, [view]);

  // Switching views: the view that was showing slides out to the left while
  // the new one slides in from the right. Both are explicit animations (not
  // CSS state classes) so the direction is always leftward however many views
  // there are; hidden views simply sit at opacity 0. Skipped on first render
  // and for prefers-reduced-motion, where the views just swap.
  useEffect(() => {
    const prev = prevActiveRef.current;
    prevActiveRef.current = active;
    if (prev === active || reduced) return;
    const enter = layerRefs.current[active];
    const leave = layerRefs.current[prev];
    const anims: Animation[] = [];
    if (enter && typeof enter.animate === "function") {
      anims.push(
        enter.animate(
          [{ opacity: 0, transform: "translateX(14%)" }, { opacity: 1, transform: "translateX(0)" }],
          { duration: SLIDE_MS, easing: "ease-out", fill: "backwards" },
        ),
      );
    }
    if (leave && typeof leave.animate === "function") {
      anims.push(
        leave.animate(
          [{ opacity: 1, transform: "translateX(0)" }, { opacity: 0, transform: "translateX(-14%)" }],
          { duration: SLIDE_MS, easing: "ease-out" },
        ),
      );
    }
    return () => anims.forEach((a) => a.cancel());
  }, [active, reduced]);

  // Entrance pan: the newly shown view starts a few days behind and glides to
  // its latest day. Only the newly active view animates, and a data refresh
  // (new object, same values) does not restart it - geometry is read through a ref.
  useEffect(() => {
    panRef.current?.cancel();
    panRef.current = null;
    const el = bodyRefs.current[active - 1];
    const layout = chartsRef.current[active - 1]?.layout;
    if (!layout || reduced || !el || typeof el.animate !== "function" || layout.endPct === layout.startPct) return;
    const anim = el.animate(
      [{ transform: `translateX(-${layout.startPct}%)` }, { transform: `translateX(-${layout.endPct}%)` }],
      { duration: PAN_MS, delay: 150, easing: "cubic-bezier(0.22, 0.61, 0.36, 1)", fill: "backwards" },
    );
    panRef.current = anim;
    return () => anim.cancel();
  }, [active, reduced]);

  const onMove = (e: React.MouseEvent<HTMLDivElement>) => {
    const rect = boxRef.current?.getBoundingClientRect();
    if (active === 0) return;
    const layout = charts[active - 1].layout;
    if (!rect || rect.width === 0 || !layout.hasValues) return;
    const viewX = ((e.clientX - rect.left) / rect.width) * CHART_VB_W;
    setHover(nearestPointIndex(layout, viewX));
  };

  const leaving = reduced ? null : view.prev;
  const viewClass = (i: number) =>
    i === active ? "opacity-100" : i === leaving ? "pointer-events-none opacity-0" : "pointer-events-none invisible opacity-0";
  const labels = ["7-Day P&L", ...charts.map((c) => c.series.label)];
  const activeData = active > 0 ? charts[active - 1] : null;

  return (
    // Outer wrapper is NOT clipped, so the tooltip can reach outside the chart box.
    <div className="relative">
    {/* Title row, OUTSIDE the chart frame: the active view's name (and its
        note, if any) on the left, the view dots on the right. */}
    <div className="mb-1 flex h-4 items-center justify-between gap-2 px-0.5">
      <div className="flex min-w-0 items-center gap-1 text-[10px] font-semibold text-[#6B7280]">
        <span className="truncate">{labels[active]}</span>
        {activeData?.note && (
          <HelpTooltip as="span" content={activeData.note} className="inline-flex shrink-0" focusable>
            <Info className="h-3 w-3 text-amber-500" aria-hidden />
            <span className="sr-only">{activeData.note}</span>
          </HelpTooltip>
        )}
      </div>
      <div className="flex shrink-0 items-center">
        {labels.map((label, i) => (
          <button
            key={label}
            type="button"
            aria-label={`Show ${label}`}
            aria-pressed={i === active}
            onClick={() => {
              setActive(i);
              setHover(null);
            }}
            className="flex h-3 w-3 items-center justify-center rounded-full focus:outline-none focus-visible:ring-1 focus-visible:ring-[#111111]/30"
          >
            <span className={clsx("h-1.5 w-1.5 rounded-full transition-colors", i === active ? "bg-[#4B5563]" : "bg-[#D1D5DB]")} />
          </button>
        ))}
      </div>
    </div>
    <div
      ref={boxRef}
      className="relative h-32 select-none overflow-hidden rounded-[10px] border border-[#E5E7EB] bg-gradient-to-b from-white to-[#F9FAFB]"
      role="img"
      aria-label={`${labels[active]}, one of ${nViews} rotating views`}
      onMouseEnter={() => {
        setPaused(true);
        panRef.current?.finish(); // hover on the settled chart, not mid-glide
      }}
      onMouseMove={onMove}
      onMouseLeave={() => {
        setPaused(false);
        setHover(null);
      }}
    >
      {/* Faint grid, as in the original chart. */}
      <svg className="absolute inset-0 h-full w-full" viewBox={`0 0 ${CHART_VB_W} ${CHART_VB_H}`} preserveAspectRatio="none" aria-hidden>
        {[0.25, 0.5, 0.75].map((frac) => {
          const gy = CHART_PAD_TOP + frac * (CHART_VB_H - CHART_PAD_TOP - CHART_PAD_BOT);
          return (
            <line key={frac} x1={CHART_PAD_X} y1={gy} x2={CHART_VB_W - CHART_PAD_X} y2={gy} stroke="#F3F4F6" strokeWidth="0.5" vectorEffect="non-scaling-stroke" />
          );
        })}
      </svg>

      {/* The views, stacked in the same frame. Only the active one is visible
          (see the slide effect above for how they swap). */}
      <div
        ref={(el) => {
          layerRefs.current[0] = el;
        }}
        aria-hidden={active !== 0}
        className={clsx("absolute inset-0", viewClass(0))}
      >
        {barsLayer}
      </div>
      {charts.map((c, ci) => {
        const i = ci + 1;
        const isActive = i === active;
        return (
          <div
            key={c.series.mode}
            ref={(el) => {
              layerRefs.current[i] = el;
            }}
            aria-hidden={!isActive}
            className={clsx("absolute inset-0", viewClass(i))}
          >
            {c.layout.hasValues ? (
              <ChartBody
                data={c}
                uid={uid}
                hover={isActive ? hover : null}
                bodyRef={(el) => {
                  bodyRefs.current[ci] = el;
                }}
              />
            ) : (
              <div className="absolute inset-0 flex items-center justify-center">
                <span className="text-[11px] text-[#9CA3AF]">No P&amp;L history yet</span>
              </div>
            )}
          </div>
        );
      })}
    </div>

    {active === 0 ? barsTooltip : activeData && hover !== null && activeData.layout.hasValues && <Tooltip data={activeData} index={hover} />}
    </div>
  );
}
