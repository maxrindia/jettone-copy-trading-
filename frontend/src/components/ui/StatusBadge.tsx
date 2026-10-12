"use client";

import clsx from "clsx";

const STATUS_STYLES: Record<string, { chip: string; dot: string }> = {
  SUCCESS: { chip: "bg-green-50 text-green-700 border-green-200", dot: "bg-green-500" },
  PENDING: { chip: "bg-[#F7F7F7] text-[#374151] border-[#E5E7EB]", dot: "bg-[#9CA3AF]" },
  SUBMITTED: { chip: "bg-[#F7F7F7] text-[#374151] border-[#E5E7EB]", dot: "bg-[#9CA3AF]" },
  REJECTED: { chip: "bg-red-50 text-red-700 border-red-200", dot: "bg-red-500" },
  FAILED: { chip: "bg-red-50 text-red-700 border-red-200", dot: "bg-red-500" },
  TIMEOUT: { chip: "bg-amber-50 text-amber-800 border-amber-200", dot: "bg-amber-500" },
  SKIPPED: { chip: "bg-gray-50 text-gray-600 border-gray-200", dot: "bg-gray-400" },
  RECONCILIATION_REQUIRED: { chip: "bg-orange-50 text-orange-700 border-orange-200", dot: "bg-jettone-orange" },
};

export default function StatusBadge({
  status,
  size = "sm",
}: {
  status: string;
  size?: "sm" | "md";
}) {
  const style = STATUS_STYLES[status] ?? {
    chip: "bg-gray-50 text-gray-600 border-gray-200",
    dot: "bg-gray-400",
  };
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1.5 rounded-full border font-semibold whitespace-nowrap",
        style.chip,
        size === "sm" ? "px-2 py-0.5 text-[10px]" : "px-2.5 py-1 text-xs",
      )}
    >
      <span className={clsx("w-1.5 h-1.5 rounded-full shrink-0", style.dot)} aria-hidden />
      {status.replace(/_/g, " ")}
    </span>
  );
}
