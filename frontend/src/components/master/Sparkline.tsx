"use client";

export default function Sparkline({
  values,
  width = 560,
  height = 120,
}: {
  values: number[];
  width?: number;
  height?: number;
}) {
  if (values.length < 2) {
    return (
      <div
        className="flex items-center justify-center bg-[#F7F7F7] border border-[#F3F4F6] rounded-lg text-xs text-[#9CA3AF]"
        style={{ height }}
      >
        No performance data available yet
      </div>
    );
  }

  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const range = max - min || 1;
  const stepX = width / (values.length - 1);

  const points = values.map((v, i) => {
    const x = i * stepX;
    const y = height - 8 - ((v - min) / range) * (height - 16);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });

  const last = values[values.length - 1];
  const first = values[0];
  const positive = last >= first;
  const stroke = positive ? "#16A34A" : "#DC2626";

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      className="w-full bg-[#F7F7F7] border border-[#F3F4F6] rounded-lg"
      style={{ height }}
      role="img"
      aria-label="Cumulative traded value chart"
    >
      <polyline
        points={points.join(" ")}
        fill="none"
        stroke={stroke}
        strokeWidth="2"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}
