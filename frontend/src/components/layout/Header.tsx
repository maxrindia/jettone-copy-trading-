"use client";

import { useState, useRef, useEffect } from "react";
import { useSession, signIn, signOut } from "next-auth/react";
import { LogOut, ArrowUpRight, Loader2 } from "lucide-react";
import { useEngineData } from "@/lib/use-engine-data";
import Button from "@/components/ui/Button";
import { setSigningOut as markSigningOut } from "@/lib/session-guard";

export default function Header() {
  const { data: session, status } = useSession();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const logoutRef = useRef<HTMLDivElement>(null);

  const {
    data: stopStatus,
    error: stopError,
  } = useEngineData<{ global_active: boolean }>("/reliability/emergency-stop", 15000);

  const emergencyStop = stopStatus?.global_active ?? false;

  // Close the logout popover on outside click or Escape (not while signing out).
  useEffect(() => {
    if (!confirmOpen) return;
    const onDown = (e: MouseEvent) => {
      if (!signingOut && logoutRef.current && !logoutRef.current.contains(e.target as Node)) {
        setConfirmOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !signingOut) setConfirmOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [confirmOpen, signingOut]);

  // Same logout flow as before (NextAuth signOut, then the sign-in page).
  const doLogout = () => {
    setSigningOut(true);
    markSigningOut(true);
    signOut({ callbackUrl: "/auth/signin" }).catch(() => {
      // Sign-out request failed: stay put and let the user try again.
      markSigningOut(false);
      setSigningOut(false);
    });
  };

  return (
    <header className="h-16 bg-white border-b border-[#E5E7EB] px-6 lg:px-8 flex items-center justify-between gap-4 sticky top-0 z-30 shrink-0">
      <div className="flex items-center gap-3 min-w-0">
        {emergencyStop && (
          <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-semibold border bg-red-50 text-red-700 border-red-200">
            <span className="relative flex w-1.5 h-1.5" aria-hidden>
              <span className="relative inline-flex w-1.5 h-1.5 rounded-full bg-red-500" />
            </span>
            TRADING HALTED
          </span>
        )}
      </div>

      <div className="flex items-center gap-3">
        {session ? (
          <div className="relative" ref={logoutRef}>
            <button
              type="button"
              onClick={() => setConfirmOpen((open) => !open)}
              aria-label="Logout"
              aria-haspopup="dialog"
              aria-expanded={confirmOpen}
              title="Logout"
              className="inline-flex h-8 w-8 items-center justify-center rounded-lg text-[#6B7280] transition-colors hover:bg-[#F7F7F7] hover:text-[#EF4444] focus:outline-none focus-visible:ring-2 focus-visible:ring-[#111111]/20"
            >
              <LogOut className="w-[18px] h-[18px]" aria-hidden />
            </button>

            {confirmOpen && (
              <div
                role="dialog"
                aria-labelledby="logout-title"
                aria-describedby="logout-desc"
                className="absolute right-0 top-full mt-2 w-60 rounded-[10px] border border-[#E5E7EB] bg-white p-3.5 shadow-lg shadow-gray-900/10 z-50 animate-in fade-in-50 zoom-in-95 duration-100"
              >
                <h2 id="logout-title" className="text-xs font-bold text-[#111111]">
                  Logout?
                </h2>
                <p id="logout-desc" className="mt-1 text-xs text-[#6B7280]">
                  Are you sure you want to logout?
                </p>
                <div className="mt-3 flex justify-end gap-2">
                  <Button type="button" variant="secondary" size="sm" onClick={() => setConfirmOpen(false)} disabled={signingOut}>
                    Cancel
                  </Button>
                  <button
                    type="button"
                    onClick={doLogout}
                    disabled={signingOut}
                    className="inline-flex items-center justify-center gap-1.5 h-8 px-3.5 rounded-lg border text-xs font-semibold shadow-sm transition-all active:scale-[0.98] bg-[#EF4444] hover:bg-[#DC2626] text-white border-[#EF4444] hover:border-[#DC2626] disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    {signingOut && <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden />}
                    {signingOut ? "Logging out…" : "Logout"}
                  </button>
                </div>
              </div>
            )}
          </div>
        ) : (
          status !== "loading" && (
            <Button size="sm" onClick={() => signIn()}>
              Sign In
              <ArrowUpRight className="w-3.5 h-3.5" aria-hidden />
            </Button>
          )
        )}
      </div>
    </header>
  );
}
