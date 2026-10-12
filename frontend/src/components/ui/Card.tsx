"use client";

import { ReactNode } from "react";
import clsx from "clsx";

export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={clsx(
        "bg-white border border-[#E5E7EB] rounded-[10px] shadow-[0_1px_2px_rgba(16,24,40,0.04)]",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function CardHeader({
  title,
  description,
  action,
  className,
  showBorder = true,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
  className?: string;
  showBorder?: boolean;
}) {
  return (
    <div
      className={clsx(
        "flex items-start justify-between gap-4 px-5 py-4",
        showBorder && "border-b border-[#F3F4F6]",
        className,
      )}
    >
      <div className="min-w-0">
        <h3 className="text-[13px] font-semibold text-[#111111] tracking-tight">{title}</h3>
        {description && <p className="text-xs text-[#6B7280] mt-0.5">{description}</p>}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-4 flex-wrap">
      <div className="min-w-0">
        <h2 className="text-lg font-bold text-[#111111] tracking-tight">{title}</h2>
        {description && <p className="text-xs text-[#6B7280] mt-1">{description}</p>}
      </div>
      {actions && <div className="flex items-center flex-wrap gap-2">{actions}</div>}
    </div>
  );
}
