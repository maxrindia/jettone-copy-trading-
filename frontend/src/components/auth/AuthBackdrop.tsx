/**
 * Purely decorative artwork for the sign-in page: a faint candlestick
 * illustration with a soft trend line, and thin waves along the bottom.
 * Everything here is drawn from fixed, made-up numbers - it is NOT market data
 * and must not be read as such. Inline SVG only (no images, no chart library),
 * hidden from assistive tech and ignored by the pointer.
 */

const VB_W = 520;
const VB_H = 500;

// Fixed illustrative shape: a dip, then a steady climb to the upper right.
const CENTER = [345, 352, 340, 350, 362, 374, 381, 366, 347, 331, 318, 300, 286, 262, 241, 233, 226, 200, 172, 150, 126, 100, 92, 84, 80, 86];
const HALF = [10, 14, 8, 12, 16, 11, 9, 15, 13, 10, 17, 12, 9, 14, 16, 10, 12, 18, 13, 11, 15, 12, 14, 9, 12, 10];
const WICK_UP = [10, 8, 14, 9, 12, 16, 8, 12, 10, 14, 9, 15, 11, 10, 13, 9, 16, 12, 10, 14, 11, 16, 9, 16, 10, 12];
const WICK_DOWN = [12, 10, 9, 14, 10, 12, 15, 9, 13, 10, 12, 9, 14, 11, 10, 15, 9, 13, 12, 10, 14, 9, 12, 10, 13, 9];
const ORANGE = new Set([5, 6, 7, 9, 15, 16, 17, 20, 21, 22]);
const X0 = 22;
const STEP = 19;
const PEAK = 21; // the candle that carries the top marker

// A lagging, smooth "average" line under the candles.
const TREND: ReadonlyArray<readonly [number, number]> = [
  [30, 432], [80, 442], [120, 458], [170, 442], [230, 384], [290, 306], [362, 262], [420, 222], [470, 184], [512, 160],
];

function smooth(points: ReadonlyArray<readonly [number, number]>): string {
  return points.reduce((d, [x, y], i) => {
    if (i === 0) return `M${x} ${y}`;
    const [px, py] = points[i - 1];
    const cx = (px + x) / 2;
    return `${d} C${cx} ${py} ${cx} ${y} ${x} ${y}`;
  }, "");
}

function Chip({ x, y, label }: { x: number; y: number; label: string }) {
  return (
    <g>
      <rect x={x} y={y} width="74" height="28" rx="7" fill="#FFFFFF" fillOpacity="0.92" stroke="#E5E7EB" />
      <text x={x + 37} y={y + 18.5} textAnchor="middle" fontSize="13" fill="#6B7280" fontFamily="Inter, system-ui, sans-serif">
        {label}
      </text>
    </g>
  );
}

export function CandlestickArt({ className }: { className?: string }) {
  const peakX = X0 + PEAK * STEP;
  const peakY = CENTER[PEAK] - HALF[PEAK] - WICK_UP[PEAK];

  return (
    <svg
      className={className}
      viewBox={`0 0 ${VB_W} ${VB_H}`}
      fill="none"
      aria-hidden="true"
      focusable="false"
      preserveAspectRatio="xMidYMid meet"
    >
      {/* barely-there grid */}
      {[70, 150, 230, 310, 390].map((y) => (
        <line key={`h${y}`} x1="0" y1={y} x2={VB_W} y2={y} stroke="#F3F4F6" strokeWidth="1" />
      ))}
      {[130, 260, 390].map((x) => (
        <line key={`v${x}`} x1={x} y1="20" x2={x} y2={VB_H - 20} stroke="#F3F4F6" strokeWidth="1" />
      ))}

      {/* secondary, softer curve */}
      <path d="M250 374 C 320 378, 380 302, 440 232 S 500 172, 520 154" stroke="#D1D5DB" strokeOpacity="0.6" strokeWidth="1.2" />

      {CENTER.map((c, i) => {
        const x = X0 + i * STEP;
        const orange = ORANGE.has(i);
        const color = orange ? "#E88900" : "#9CA3AF";
        return (
          <g key={i} opacity={orange ? 0.42 : 0.3}>
            <line x1={x} y1={c - HALF[i] - WICK_UP[i]} x2={x} y2={c + HALF[i] + WICK_DOWN[i]} stroke={color} strokeWidth="1.2" strokeLinecap="round" />
            <rect x={x - 5.5} y={c - HALF[i]} width="11" height={HALF[i] * 2} rx="1.5" fill={color} />
          </g>
        );
      })}

      {/* soft orange trend */}
      <path d={smooth(TREND)} stroke="#E88900" strokeOpacity="0.5" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx={TREND[2][0]} cy={TREND[2][1]} r="4" fill="#E88900" fillOpacity="0.8" />
      <circle cx={TREND[6][0]} cy={TREND[6][1]} r="4" fill="#E88900" fillOpacity="0.8" />
      <circle cx={peakX} cy={peakY} r="4" fill="#E88900" fillOpacity="0.8" />

      {/* sparse illustrative price chips */}
      <Chip x={peakX - 86} y={peakY - 40} label="264.50" />
      <Chip x={TREND[6][0] + 2} y={TREND[6][1] + 6} label="259.10" />
      <Chip x={TREND[2][0] + 14} y={TREND[2][1] + 6} label="245.30" />
    </svg>
  );
}

const WAVE_LINES = 22;

function wavePath(i: number, w: number, h: number): string {
  // A twisting ribbon: every line shares the same flow, offset a little in
  // phase and spread, so the bundle narrows and fans out along the page.
  const t = i / (WAVE_LINES - 1);
  const pts: string[] = [];
  for (let x = 0; x <= w; x += 32) {
    const u = (x / w) * Math.PI * 2;
    const spread = (t - 0.5) * h * 0.42 * (0.55 + 0.45 * Math.cos(u * 0.9 + 0.6));
    const y = h * 0.58 + Math.sin(u * 1.05 + t * 1.1) * (h * 0.16) + Math.sin(u * 2.3 + 1.2) * (h * 0.05) + spread;
    pts.push(`${x} ${y.toFixed(1)}`);
  }
  return "M" + pts.join(" L");
}

export function WavesArt({ className }: { className?: string }) {
  const w = 1600;
  const h = 260;
  return (
    <svg className={className} viewBox={`0 0 ${w} ${h}`} fill="none" aria-hidden="true" focusable="false" preserveAspectRatio="none">
      {Array.from({ length: WAVE_LINES }, (_, i) => (
        <path key={i} d={wavePath(i, w, h)} stroke="#9CA3AF" strokeOpacity="0.11" strokeWidth="0.7" strokeLinejoin="round" />
      ))}
      <path d={wavePath(7, w, h)} stroke="#E88900" strokeOpacity="0.2" strokeWidth="0.9" />
      <path d={wavePath(15, w, h)} stroke="#E88900" strokeOpacity="0.12" strokeWidth="0.9" />
    </svg>
  );
}
