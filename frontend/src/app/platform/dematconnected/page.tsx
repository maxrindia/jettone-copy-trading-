"use client";

import { useEffect, useState, Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { CheckCircle2, Loader2, XCircle } from "lucide-react";
import { consumeReconnectTarget } from "@/lib/alice-blue-login";

function DematConnectedContent() {
  const searchParams = useSearchParams();
  const [status, setStatus] = useState<"loading" | "success" | "error">("loading");
  const [message, setMessage] = useState("Authenticating Alice Blue connection...");

  useEffect(() => {
    const authCode = searchParams.get("authCode") || searchParams.get("code") || searchParams.get("auth_code");

    if (!authCode) {
      // If no authCode returned (login was not completed)
      setStatus("error");
      setMessage("No authorization code received from Alice Blue. Please try logging in again.");
      return;
    }

    async function processAuthCode() {
      try {
        // Single-use: this also clears the stored target so a reload of this
        // tab, or a second concurrent reconnect, can't reuse or clobber it.
        const reconnectTarget = consumeReconnectTarget();
        const accountId = searchParams.get("account_id") || reconnectTarget.accountId;
        const targetUserId = reconnectTarget.aliceUserId;
        const returnedUserId = searchParams.get("userId") || searchParams.get("user_id") || null;

        const resp = await fetch("/api/v1/accounts/callback", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            auth_code: authCode,
            account_id: accountId,
            target_user_id: targetUserId,
            returned_user_id: returnedUserId,
          }),
        });

        const data = await resp.json().catch(() => null);

        if (resp.ok) {
          setStatus("success");
          setMessage(data?.message || "Alice Blue session verified successfully!");
          notifyParentAndClose();
        } else {
          setStatus("error");
          // The /api/v1/* proxy (route.ts) wraps engine errors as
          // { success: false, error: { code, message } }, not the raw
          // FastAPI { detail: "..." } shape — check both so the actual
          // reason (e.g. "Account mismatch: ...") reaches the user instead
          // of the generic fallback below.
          const errDetail =
            typeof data?.error?.message === "string" ? data.error.message :
            typeof data?.detail === "string" ? data.detail :
            typeof data?.detail === "object" && data.detail !== null ? JSON.stringify(data.detail) :
            null;
          setMessage(errDetail || data?.message || `Failed to authenticate session (HTTP ${resp.status})`);
        }
      } catch {
        setStatus("error");
        setMessage("Connection failed. Please check network and try again.");
      }
    }

    processAuthCode();
  }, [searchParams]);

  function notifyParentAndClose() {
    if (typeof window !== "undefined") {
      // Signal the opener/parent tab via localStorage (works for both popup and new-tab flow)
      localStorage.setItem("demat_connected", Date.now().toString());
      
      // Also try postMessage for legacy popup support
      if (window.opener) {
        try {
          window.opener.postMessage({ type: "DEMAT_CONNECTED" }, "*");
        } catch {
          // opener may be cross-origin or gone
        }
      }
      
      // Close this tab after short delay
      setTimeout(() => {
        window.close();
        // If window.close() is blocked (new tab not opened by script), redirect back
        setTimeout(() => {
          window.location.href = "/demat";
        }, 500);
      }, 1500);
    }
  }

  return (
    <div className="min-h-[100dvh] bg-[#F7F7F7] flex items-center justify-center p-4">
      <div className="w-full max-w-sm bg-white border border-[#E5E7EB] rounded-[10px] p-6 text-center shadow-lg space-y-4">
        {status === "loading" && (
          <>
            <Loader2 className="w-12 h-12 text-jettone-orange animate-spin mx-auto" />
            <h2 className="text-base font-extrabold text-[#111111]">Connecting Account</h2>
            <p className="text-xs text-[#6B7280]">{message}</p>
          </>
        )}

        {status === "success" && (
          <>
            <CheckCircle2 className="w-12 h-12 text-green-500 mx-auto" />
            <h2 className="text-base font-extrabold text-[#111111]">Success!</h2>
            <p className="text-xs font-medium text-green-700 bg-green-50 border border-green-200 rounded-[10px] py-2 px-3">
              {message}
            </p>
            <p className="text-[11px] text-[#9CA3AF]">This window will close automatically...</p>
          </>
        )}

        {status === "error" && (
          <>
            <XCircle className="w-12 h-12 text-red-500 mx-auto" />
            <h2 className="text-base font-extrabold text-[#111111]">Connection Failed</h2>
            <p className="text-xs font-medium text-red-700 bg-red-50 border border-red-200 rounded-[10px] py-2.5 px-3 leading-relaxed text-left">
              {message}
            </p>
            <div className="flex flex-col gap-2 pt-2">
              <a
                href="https://ant.aliceblueonline.com/"
                target="_blank"
                rel="noreferrer"
                className="w-full inline-flex items-center justify-center gap-1.5 h-8 px-3.5 rounded-lg border text-xs font-semibold shadow-sm transition-all active:scale-[0.98] bg-amber-600 hover:bg-amber-700 text-white border-amber-600 hover:border-amber-700"
              >
                1. Open Alice Blue to Logout
              </a>
              <button
                onClick={() => window.close()}
                className="w-full inline-flex items-center justify-center gap-1.5 h-8 px-3.5 rounded-lg border text-xs font-semibold shadow-sm transition-all active:scale-[0.98] bg-jettone-orange hover:bg-jettone-orange-hover text-white border-jettone-orange hover:border-jettone-orange-hover"
              >
                2. Close & Retry Re-login
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export default function DematConnectedPage() {
  return (
    <Suspense fallback={<div className="p-8 text-center text-sm font-semibold">Loading connection status...</div>}>
      <DematConnectedContent />
    </Suspense>
  );
}
