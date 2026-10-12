"use client";

import { ButtonHTMLAttributes, forwardRef } from "react";
import { Loader2 } from "lucide-react";
import clsx from "clsx";

type Variant = "primary" | "secondary" | "destructive" | "success" | "ghost";
type Size = "sm" | "md";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
}

const variantClasses: Record<Variant, string> = {
  primary:
    "bg-jettone-orange text-white hover:bg-jettone-orange-hover active:bg-jettone-orange-hover shadow-sm border border-transparent",
  secondary:
    "bg-white text-[#374151] border border-[#E5E7EB] hover:bg-[#F9FAFB] hover:text-[#111111] shadow-sm",
  destructive:
    "bg-red-600 text-white hover:bg-red-700 active:bg-red-800 shadow-sm border border-transparent",
  success:
    "bg-green-600 text-white hover:bg-green-700 active:bg-green-800 shadow-sm border border-transparent",
  ghost:
    "bg-transparent text-[#4B5563] border border-transparent hover:bg-[#F3F4F6] hover:text-[#111111]",
};

const sizeClasses: Record<Size, string> = {
  sm: "h-8 px-3.5 text-xs rounded-lg gap-1.5",
  md: "h-9.5 px-4 py-2 text-[13px] rounded-lg gap-2",
};

const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "primary", size = "md", loading = false, className, children, disabled, ...props },
  ref,
) {
  return (
    <button
      ref={ref}
      disabled={disabled || loading}
      className={clsx(
        "inline-flex items-center justify-center font-semibold transition-colors select-none",
        "disabled:opacity-50 disabled:cursor-not-allowed",
        variantClasses[variant],
        sizeClasses[size],
        className,
      )}
      {...props}
    >
      {loading && <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden />}
      {children}
    </button>
  );
});

export default Button;
