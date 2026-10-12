"use client";

import { Loader2, TriangleAlert, Inbox, RotateCw } from "lucide-react";
import Button from "@/components/ui/Button";

export function LoadingPanel({ label = "Loading live data…", className = "" }: { label?: string; className?: string }) {
  return (
    <div className={`p-10 rounded-[10px] bg-white border border-[#E5E7EB] text-center ${className}`}>
      <Loader2 className="w-5 h-5 animate-spin text-[#9CA3AF] mx-auto" aria-hidden />
      <p className="text-xs text-[#6B7280] mt-3">{label}</p>
    </div>
  );
}

export function ErrorPanel({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div
      role="alert"
      className="p-4 rounded-[10px] bg-red-50 border border-red-200 flex items-center justify-between gap-4"
    >
      <div className="flex items-center gap-2.5 min-w-0">
        <TriangleAlert className="w-4 h-4 text-red-600 shrink-0" aria-hidden />
        <p className="text-xs font-medium text-red-700 truncate">{message}</p>
      </div>
      {onRetry && (
        <Button variant="secondary" size="sm" onClick={onRetry} className="!text-red-700 shrink-0">
          <RotateCw className="w-3 h-3" aria-hidden />
          Retry
        </Button>
      )}
    </div>
  );
}

export function EmptyState({
  title,
  hint,
  icon,
  className = "",
}: {
  title: string;
  hint?: string;
  icon?: React.ReactNode;
  /** Extra classes, e.g. a page-specific corner radius. */
  className?: string;
}) {
  return (
    <div className={`p-10 rounded-[10px] bg-white border border-dashed border-[#E5E7EB] text-center space-y-2 ${className}`}>
      <div className="flex justify-center">
        {icon ?? <Inbox className="w-6 h-6 text-[#D1D5DB]" aria-hidden />}
      </div>
      <p className="text-sm font-semibold text-[#374151]">{title}</p>
      {hint && <p className="text-xs text-[#9CA3AF] max-w-md mx-auto">{hint}</p>}
    </div>
  );
}
