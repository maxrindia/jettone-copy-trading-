"use client";

import { TriangleAlert, CircleAlert, Info } from "lucide-react";

interface AlertBannerProps {
  type?: "warning" | "error" | "info";
  message: string;
  actionText?: string;
  onAction?: () => void;
}

const styles = {
  warning: "bg-amber-50 border-amber-200 text-amber-900",
  error: "bg-red-50 border-red-200 text-red-900",
  info: "bg-[#F7F7F7] border-[#E5E7EB] text-[#111111]",
};

const icons = {
  warning: TriangleAlert,
  error: CircleAlert,
  info: Info,
};

const iconColors = {
  warning: "text-amber-600",
  error: "text-red-600",
  info: "text-[#6B7280]",
};

export default function AlertBanner({
  type = "warning",
  message,
  actionText,
  onAction,
}: AlertBannerProps) {
  const Icon = icons[type];
  return (
    <div
      role={type === "error" ? "alert" : "status"}
      className={`p-4 rounded-[10px] border flex items-center justify-between gap-4 ${styles[type]}`}
    >
      <div className="flex items-center gap-3 min-w-0">
        <Icon className={`w-4 h-4 shrink-0 ${iconColors[type]}`} aria-hidden />
        <span className="text-xs font-medium truncate">{message}</span>
      </div>
      {actionText && onAction && (
        <button
          onClick={onAction}
          className="px-3 py-1.5 rounded-lg text-xs font-semibold bg-white hover:bg-gray-50 border border-gray-200 text-[#111111] shadow-sm transition-colors shrink-0"
        >
          {actionText}
        </button>
      )}
    </div>
  );
}
