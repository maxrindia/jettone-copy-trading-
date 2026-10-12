/** Shared by the intraday (Today) and per-day (other ranges) Strategy P&L charts. */

// Categorical series colours (a strategy keeps its colour by its position).
export const COLORS = ["#E88900", "#111111", "#6B7280", "#B45309", "#9CA3AF"];

export type PathPoint = { x: number; y: number };

/**
 * Monotone cubic Hermite interpolation (Fritsch-Carlson), converted to SVG
 * cubic-bezier path commands. Passes through every point EXACTLY (same
 * anchors the straight-line version used) and is mathematically guaranteed
 * never to overshoot beyond the two values it's curving between - unlike a
 * naive Catmull-Rom spline, it cannot render a bump implying a P&L extreme
 * that never actually occurred. This is the same interpolation D3 calls
 * curveMonotoneX and is the standard "safe" smoothing for financial data.
 */
export function buildMonotonePath(pts: PathPoint[]): string {
  const n = pts.length;
  if (n === 0) return "";
  if (n === 1) return `M ${pts[0].x} ${pts[0].y}`;

  const dx: number[] = [];
  const slope: number[] = [];
  for (let i = 0; i < n - 1; i++) {
    const deltaX = pts[i + 1].x - pts[i].x;
    const deltaY = pts[i + 1].y - pts[i].y;
    dx.push(deltaX);
    slope.push(deltaX === 0 ? 0 : deltaY / deltaX);
  }

  const tangent: number[] = new Array(n).fill(0);
  tangent[0] = slope[0];
  tangent[n - 1] = slope[n - 2];
  for (let i = 1; i < n - 1; i++) {
    const sameSign = (slope[i - 1] < 0) === (slope[i] < 0);
    tangent[i] = slope[i - 1] === 0 || slope[i] === 0 || !sameSign ? 0 : (slope[i - 1] + slope[i]) / 2;
  }

  // Fritsch-Carlson constraint: clamp each interval's tangents so the curve
  // stays monotonic between its two endpoints.
  for (let i = 0; i < n - 1; i++) {
    if (slope[i] === 0) {
      tangent[i] = 0;
      tangent[i + 1] = 0;
      continue;
    }
    const a = Math.max(0, tangent[i] / slope[i]);
    const b = Math.max(0, tangent[i + 1] / slope[i]);
    const dist = a * a + b * b;
    if (dist > 9) {
      const tau = 3 / Math.sqrt(dist);
      tangent[i] = tau * a * slope[i];
      tangent[i + 1] = tau * b * slope[i];
    }
  }

  let d = `M ${pts[0].x} ${pts[0].y}`;
  for (let i = 0; i < n - 1; i++) {
    const h = dx[i] / 3;
    const cp1x = pts[i].x + h;
    const cp1y = pts[i].y + tangent[i] * h;
    const cp2x = pts[i + 1].x - h;
    const cp2y = pts[i + 1].y - tangent[i + 1] * h;
    d += ` C ${cp1x} ${cp1y}, ${cp2x} ${cp2y}, ${pts[i + 1].x} ${pts[i + 1].y}`;
  }
  return d;
}

/** The same monotone line, closed down to a baseline y - for the soft area fill. */
export function buildAreaPath(pts: PathPoint[], baselineY: number): string {
  if (pts.length === 0) return "";
  const line = buildMonotonePath(pts);
  const first = pts[0];
  const last = pts[pts.length - 1];
  return `${line} L ${last.x} ${baselineY} L ${first.x} ${baselineY} Z`;
}
