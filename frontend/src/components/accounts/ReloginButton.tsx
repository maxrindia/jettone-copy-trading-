import { ExternalLink } from "lucide-react";

/**
 * The account card's footer "Re-login" button. Idle, it is byte-for-byte the button the card always had; while a
 * login for this account is running it is disabled (so a repeated click cannot start a second request) and says so.
 */
export default function ReloginButton({ onRelogin, busy = false }: { onRelogin: () => void | Promise<void>; busy?: boolean }) {
  return (
    <button
      onClick={() => onRelogin()}
      disabled={busy}
      aria-busy={busy || undefined}
      className="inline-flex items-center justify-center gap-1.5 h-8 px-3.5 rounded-lg border text-xs font-semibold shadow-sm transition-all active:scale-[0.98] bg-[#2563EB] hover:bg-[#1D4ED8] text-white border-[#2563EB] hover:border-[#1D4ED8] disabled:opacity-60 disabled:cursor-not-allowed"
    >
      <ExternalLink className="w-3.5 h-3.5" />
      {busy ? "Logging in..." : "Re-login"}
    </button>
  );
}
