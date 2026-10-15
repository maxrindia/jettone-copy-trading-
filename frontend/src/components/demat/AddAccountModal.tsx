"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { X, Info, Copy, Check } from "lucide-react";
import { copyToClipboard } from "@/lib/clipboard";
import { extractError } from "@/lib/use-engine-data";
import OptionDropdown, { type DropdownGroup } from "@/components/ui/OptionDropdown";
import DatePicker from "@/components/ui/DatePicker";
import { normalizeProxyHost } from "@/lib/proxy-host";
import { buildProxyUrl, parseProxyUrl, validateProxyUrlInput } from "@/lib/proxy-config";
import { isValidAssignedIp, isValidExpiryInput } from "@/lib/static-ip";
import { buildAccountUpdateBody, createSubmitGuard, runSubmit, type SubmitMode } from "@/lib/account-update";
import { confirmAliceBlueLogin, recordReconnectIntent, setReconnectTarget } from "@/lib/alice-blue-login";
import Tooltip from "@/components/ui/Tooltip";

export interface AddDematPayload {
  account_type: "MASTER" | "FOLLOWER";
  display_name: string;
  alice_user_id: string;
  api_secret: string;
  broker?: string;
  mobile?: string;
  email?: string;
  connect_with_ip?: boolean;
  ip_address?: string;
}

export interface EditDematAccountData {
  id?: string;
  display_name: string;
  alice_user_id: string;
  notes?: string | null;
  proxy_expires_on?: string | null;
  static_ip_address?: string | null;
}

interface AddAccountModalProps {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
  /** Called after the account details were saved (by Save, or by Save & Reconnect before its reconnect step). */
  onSaved?: () => void;
  accountToEdit?: EditDematAccountData | null;
}

const BROKER_GROUPS: readonly DropdownGroup<string>[] = [
  {
    label: "Popular Discount & Zero Brokerage",
    options: [
      { value: "alicebluenew", label: "Alice Blue (ANT / A3)" },
      { value: "flattrade", label: "Flattrade (WallConnect - Zero Brokerage)" },
      { value: "shoonya", label: "Finvasia Shoonya (Zero Brokerage)" },
      { value: "zerodha", label: "Zerodha (Kite Connect)" },
      { value: "angelone", label: "Angel One (SmartAPI)" },
      { value: "dhan", label: "Dhan (DhanHQ)" },
      { value: "fyers", label: "Fyers (Fyers API v3)" },
      { value: "upstox", label: "Upstox (Upstox v2)" },
      { value: "groww", label: "Groww" },
      { value: "5paisa", label: "5Paisa" },
    ],
  },
  {
    label: "Full-Service & Banking Brokers",
    options: [
      { value: "kotakneo", label: "Kotak Neo" },
      { value: "icicidirect", label: "ICICI Direct (Breeze)" },
      { value: "hdfcsky", label: "HDFC Sky" },
    ],
  },
];

const CONNECTION_TYPES = [
  { value: "host", label: "Host / IP" },
  { value: "url", label: "Proxy URL" },
] as const;
type ConnectionType = (typeof CONNECTION_TYPES)[number]["value"];

export default function AddAccountModal({ open, onClose, onCreated, onSaved, accountToEdit }: AddAccountModalProps) {
  const [mounted, setMounted] = useState(false);
  const [redirectUrl, setRedirectUrl] = useState("");
  const [broker, setBroker] = useState("");
  const [nickname, setNickname] = useState("");
  const [mobile, setMobile] = useState("");
  const [email, setEmail] = useState("");
  const [connectWithIp, setConnectWithIp] = useState(false);
  const [ipAddress, setIpAddress] = useState("");
  const [brokerUserId, setBrokerUserId] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [password, setPassword] = useState("");
  const [totpKey, setTotpKey] = useState("");
  const [accountType, setAccountType] = useState<"MASTER" | "FOLLOWER">("FOLLOWER");
  const [staticIpEnabled, setStaticIpEnabled] = useState(false);
  const [proxyHost, setProxyHost] = useState("");
  const [proxyPort, setProxyPort] = useState("443");
  const [proxyUsername, setProxyUsername] = useState("");
  const [proxyPassword, setProxyPassword] = useState("");
  const [proxyScheme, setProxyScheme] = useState<"http" | "https">("https");
  const [connectionType, setConnectionType] = useState<ConnectionType>("host");
  const [proxyUrlInput, setProxyUrlInput] = useState("");
  const [proxyExpiry, setProxyExpiry] = useState("");
  const [assignedIp, setAssignedIp] = useState("");
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitMode, setSubmitMode] = useState<SubmitMode>("reconnect");
  // Synchronous re-entry guard: at most one save/reconnect runs at a time.
  const submitGuard = useRef(createSubmitGuard());

  useEffect(() => {
    setMounted(true);
    if (typeof window !== "undefined") {
      setRedirectUrl(`${window.location.origin}/platform/dematconnected`);
    }

    // VPS server IP is fixed — all API/WebSocket calls originate from VPS, not browser
  }, []);

  // Lock background body scroll when drawer is open
  useEffect(() => {
    if (!open) return;

    const originalOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !submitting) onClose();
    };
    window.addEventListener("keydown", onKey);

    return () => {
      document.body.style.overflow = originalOverflow;
      window.removeEventListener("keydown", onKey);
    };
  }, [open, onClose, submitting]);

  useEffect(() => {
    if (open) {
      setError(null);
      setCopied(false);
      if (typeof window !== "undefined") {
        setRedirectUrl(`${window.location.origin}/platform/dematconnected`);
      }

      if (accountToEdit) {
        setNickname(accountToEdit.display_name || "");
        setBrokerUserId(accountToEdit.alice_user_id || "");

        let brokerVal = "alicebluenew";
        let key = "";
        let sec = "";
        let mob = "";
        let em = "";
        if (accountToEdit.notes) {
          const brokerMatch = accountToEdit.notes.match(/Broker:\s*([^\s,]+)/i);
          if (brokerMatch) brokerVal = brokerMatch[1];
          const keyMatch = accountToEdit.notes.match(/APIKey:\s*([^\s,]+)/);
          if (keyMatch) key = keyMatch[1];
          const secMatch = accountToEdit.notes.match(/APISecret:\s*([^\s,]+)/);
          if (secMatch) sec = secMatch[1];
          const mobMatch = accountToEdit.notes.match(/Mobile:\s*([^\s,]+)/);
          if (mobMatch) mob = mobMatch[1];
          const emMatch = accountToEdit.notes.match(/Email:\s*([^\s,]+)/);
          if (emMatch) em = emMatch[1];
        }
        setBroker(brokerVal);
        setApiKey(key);
        setApiSecret(sec);
        setMobile(mob);
        setEmail(em);
        // Parse ProxyURL into its 4 fields - never shown as a raw URL.
        const proxyMatch = accountToEdit.notes?.match(/ProxyURL:\s*([^,]+)/);
        const parsedProxy = proxyMatch ? parseProxyUrl(proxyMatch[1].trim()) : null;
        setConnectionType("host");
        setProxyUrlInput("");
        setProxyExpiry(accountToEdit.proxy_expires_on ?? "");
        setAssignedIp(accountToEdit.static_ip_address ?? "");
        if (parsedProxy) {
          setStaticIpEnabled(true);
          setProxyScheme(parsedProxy.scheme ?? "https");
          setProxyHost(parsedProxy.host);
          setProxyPort(parsedProxy.port);
          setProxyUsername(parsedProxy.username);
          setProxyPassword(parsedProxy.password);
        } else {
          setStaticIpEnabled(false);
          setProxyHost("");
          setProxyPort("443");
          setProxyUsername("");
          setProxyPassword("");
        }
      } else {
        setBroker("");
        setNickname("");
        setMobile("");
        setEmail("");
        setConnectWithIp(false);
        setIpAddress("");
        setAssignedIp("");
        setBrokerUserId("");
        setApiKey("");
        setApiSecret("");
        setTotpKey("");
        setStaticIpEnabled(false);
        setProxyScheme("https");
        setProxyHost("");
        setProxyPort("443");
        setProxyUsername("");
        setProxyPassword("");
        setConnectionType("host");
        setProxyUrlInput("");
        setProxyExpiry("");
      }
    }
  }, [open, accountToEdit]);

  if (!mounted || !open) return null;

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void submit("reconnect");
  };

  // "save" persists the edited details only (no broker login, no session, no
  // redirect). "reconnect" saves first and starts the Alice Blue login only
  // after the save succeeded.
  const submit = async (mode: SubmitMode) => {
    if (submitGuard.current.busy) return;
    setError(null);

    if (!broker) {
      setError("Please select a Broker.");
      return;
    }

    if (!nickname.trim()) {
      setError("Please enter a Nickname / Account name.");
      return;
    }

    // Resolves to the ProxyURL string the backend stores (null when Static IP is off).
    let proxyUrlToStore: string | null = null;
    if (staticIpEnabled) {
      if (!isValidExpiryInput(proxyExpiry)) {
        setError("Please enter a valid Static IP expiry date.");
        return;
      }
      if (connectionType === "url") {
        const checked = validateProxyUrlInput(proxyUrlInput);
        if (!checked.ok) {
          setError(checked.error);
          return;
        }
        proxyUrlToStore = checked.url;
      } else {
        if (!proxyHost.trim()) {
          setError("Please enter the Static IP (an IPv4 address or hostname).");
          return;
        }
        const typedHost = normalizeProxyHost(proxyHost);
        if (!typedHost) {
          setError("Please enter a valid Static IP (an IPv4 address or hostname, e.g. 178.18.252.165).");
          return;
        }
        if (!proxyUsername.trim()) {
          setError("Please enter the Static IP username.");
          return;
        }
        if (!proxyPassword.trim()) {
          setError("Please enter the Static IP password.");
          return;
        }
        const portText = (typedHost.port ?? proxyPort).trim();
        const portNum = Number(portText);
        if (!portText || !Number.isInteger(portNum) || portNum <= 0 || portNum > 65535) {
          setError("Please enter a valid Static IP port (1-65535).");
          return;
        }
        // The user types only the host/IP; the ProxyURL the backend expects is built from it plus
        // the Port / Username / Password fields (see buildProxyUrl).
        proxyUrlToStore = buildProxyUrl({
          host: typedHost.host,
          port: portText,
          username: proxyUsername.trim(),
          password: proxyPassword.trim(),
          scheme: proxyScheme,
        });
      }
    }
    // Date only (YYYY-MM-DD, IST calendar). null clears it: "expiry not tracked".
    const expiryToStore = staticIpEnabled && proxyExpiry ? proxyExpiry : null;
    // The public IP StaticIP.in assigned to this account (null clears it). Never the gateway host.
    if (staticIpEnabled && !isValidAssignedIp(assignedIp)) {
      setError("Assigned public IP must be an IPv4 or IPv6 address, not a hostname.");
      return;
    }
    const assignedIpToStore = staticIpEnabled && assignedIp.trim() ? assignedIp.trim() : null;

    if (!submitGuard.current.tryEnter()) return;
    setSubmitting(true);
    setSubmitMode(mode);

    const notesString = [
      broker ? `Broker: ${broker}` : null,
      mobile ? `Mobile: ${mobile}` : null,
      email ? `Email: ${email}` : null,
      apiKey ? `APIKey: ${apiKey}` : null,
      apiSecret ? `APISecret: ${apiSecret}` : null,
      proxyUrlToStore ? `ProxyURL: ${proxyUrlToStore}` : null,
    ].filter(Boolean).join(", ");
    const editing = Boolean(accountToEdit && accountToEdit.id);

    try {
      const outcome = await runSubmit(
        mode,
        // Step 1 - save the latest edited details (existing update / create API).
        async () => {
          let resp;
          if (accountToEdit && accountToEdit.id) {
            resp = await fetch(`/api/v1/accounts/${accountToEdit.id}`, {
              method: "PUT",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(
                buildAccountUpdateBody({
                  displayName: nickname.trim(),
                  broker,
                  notes: notesString,
                  proxyExpiresOn: expiryToStore,
                  staticIpAddress: assignedIpToStore,
                }),
              ),
            });
          } else {
            resp = await fetch("/api/v1/accounts", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                account_type: accountType,
                display_name: nickname.trim(),
                alice_user_id: brokerUserId.trim() || `ACC${Math.floor(100000 + Math.random() * 900000)}`,
                api_secret: apiSecret.trim() || "placeholder_secret",
                broker: broker,
                notes: notesString,
                proxy_expires_on: expiryToStore,
                static_ip_address: assignedIpToStore,
              }),
            });
          }

          if (!resp.ok) {
            const body = (await resp.json().catch(() => null)) as unknown;
            throw new Error(extractError(body) ?? `Failed (${resp.status})`);
          }

          const resData = await resp.json().catch(() => null);
          return {
            id: (resData?.id as string | undefined) ?? accountToEdit?.id,
            userId: (resData?.alice_user_id as string | undefined) ?? (brokerUserId.trim() || accountToEdit?.alice_user_id),
          };
        },
        // Step 2 - only reached after the save succeeded: the Alice Blue login redirect.
        async (saved) => {
          // Open Alice Blue ANT Web Login in new tab (bypasses popup blockers) if broker is Alice Blue
          const appCodeToUse = apiKey.trim() || "uCKZ00C9th";
          const uIdToUse = (saved.userId || brokerUserId).trim();
          if (
            typeof window !== "undefined" &&
            saved.id &&
            (broker === "alicebluenew" || broker === "Alice Blue") &&
            confirmAliceBlueLogin(uIdToUse || "this account")
          ) {
            // Bind target account context for DematConnected callback verification.
            // Only set once the user has actually confirmed the redirect, so a
            // cancelled confirm doesn't leave a stale reconnect target behind.
            setReconnectTarget(saved.id, saved.userId);
            await recordReconnectIntent(saved.id);

            const popupUrl = `https://ant.aliceblueonline.com/?appcode=${encodeURIComponent(appCodeToUse)}${uIdToUse ? `&userId=${encodeURIComponent(uIdToUse)}` : ""}`;
            const a = document.createElement("a");
            a.href = popupUrl;
            a.target = "_blank";
            a.rel = "noreferrer noopener";
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
          }
        },
      );

      switch (outcome.status) {
        case "saved":
          // Details saved; no login was started. The page shows the success feedback.
          onSaved?.();
          onClose();
          break;
        case "reconnected":
          onCreated();
          onClose();
          break;
        case "save_failed":
          // Nothing was reconnected; keep the dialog open so the details can be corrected.
          setError(editing ? `Could not save the account details: ${outcome.message}` : outcome.message);
          break;
        case "reconnect_failed":
          // The details ARE saved; only the login could not be started.
          onSaved?.();
          setError(
            `${editing ? "Your details were saved" : "The account was created"}, but the reconnect could not be started: ${outcome.message}`,
          );
          break;
      }
    } finally {
      submitGuard.current.leave();
      setSubmitting(false);
    }
  };


  return createPortal(
    <div className="fixed inset-0 z-[9999] flex justify-end pointer-events-auto">
      {/* Background Dim & Backdrop Blur Overlay */}
      <div
        className="fixed inset-0 bg-black/40 backdrop-blur-sm transition-opacity duration-200 animate-in fade-in"
        onClick={() => !submitting && onClose()}
        aria-hidden="true"
      />

      {/* Full-Height Right-Side Drawer Panel (Existing Theme: Crisp White & Black) */}
      <div
        className="relative z-10 w-full max-w-lg h-full bg-white border-l border-[#E5E7EB] text-[#111111] shadow-2xl flex flex-col justify-between box-border animate-in slide-in-from-right duration-300 ease-out"
        role="dialog"
        aria-modal="true"
      >
        {/* Fixed Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-[#E5E7EB] bg-[#F7F7F7] shrink-0">
          <h2 className="text-sm font-bold text-[#111111] tracking-tight">
            {accountToEdit ? "Reconnect Demat Account" : "Add Demat Account"}
          </h2>
          <button
            onClick={() => !submitting && onClose()}
            className="p-1.5 rounded-lg text-[#9CA3AF] hover:text-[#111111] hover:bg-gray-100 transition-colors"
            title="Close panel"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Scrollable Form Content */}
        <div className="flex-1 min-h-0 overflow-y-auto p-6 space-y-4">
          <form
            id="add-demat-form"
            onSubmit={handleSubmit}
            className="space-y-4 [&_input::placeholder]:font-sans [&_input::placeholder]:text-xs [&_input::placeholder]:font-normal [&_input::placeholder]:text-[#9CA3AF] [&_input::placeholder]:opacity-100"
          >
            {error && (
              <div className="p-3 rounded-[10px] bg-red-50 border border-red-200 text-xs font-medium text-red-700">
                {error}
              </div>
            )}

            {/* Top Redirect URL Banner */}
            <div className="p-3 rounded-[10px] bg-[#F9FAFB] border border-[#E5E7EB] flex items-center justify-between gap-2">
              <div className="text-xs text-[#4B5563] truncate">
                <span className="font-semibold text-[#111111]">Redirect URL: </span>
                <span className="text-[#374151] font-normal">{redirectUrl}</span>
              </div>
              <Tooltip content={copyFailed ? "Copy failed - select the URL and copy it manually" : "Copy Redirect URL"}><button
                type="button"
                onClick={async () => {
                  const ok = await copyToClipboard(redirectUrl);
                  setCopied(ok);
                  setCopyFailed(!ok);
                  setTimeout(() => {
                    setCopied(false);
                    setCopyFailed(false);
                  }, 2000);
                }}
                disabled={!redirectUrl}
                className="p-1.5 rounded-lg text-[#9CA3AF] hover:text-[#111111] hover:bg-gray-200 transition-colors shrink-0 disabled:opacity-50"
                aria-label="Copy Redirect URL"
              >
                {copied ? (
                  <Check className="w-3.5 h-3.5 text-green-600" />
                ) : copyFailed ? (
                  <X className="w-3.5 h-3.5 text-red-600" />
                ) : (
                  <Copy className="w-3.5 h-3.5" />
                )}
              </button></Tooltip>
            </div>

            {/* Field 1: Broker */}
            <div>
              <label className="block text-[11px] font-semibold text-[#4B5563] mb-1.5 uppercase tracking-wider">
                Broker *
              </label>
              <OptionDropdown
                id="broker-select"
                ariaLabel="Broker"
                value={broker}
                onChange={setBroker}
                groups={BROKER_GROUPS}
                placeholder="Select Broker"
                fullWidth
                align="left"
                selectedTextClass="text-[#111111]"
                optionTextClass="text-[#111111]"
              />
            </div>

            {/* Dynamic Broker Specific Credentials Section */}
            {broker && (
              <div className="pt-2 space-y-3 animate-in fade-in duration-200">
                <div className="flex items-center justify-between">
                  <h3 className="text-xs font-bold text-[#111111] tracking-tight flex items-center gap-1.5">
                    Enter {
                      broker === "alicebluenew" ? "Alice Blue (A3)" :
                      broker === "flattrade" ? "Flattrade" :
                      broker === "zerodha" ? "Zerodha Kite" :
                      broker === "angelone" ? "Angel One SmartAPI" :
                      broker === "dhan" ? "Dhan" :
                      broker === "fyers" ? "Fyers" :
                      broker === "shoonya" ? "Finvasia Shoonya" :
                      broker === "upstox" ? "Upstox" :
                      broker === "kotakneo" ? "Kotak Neo" :
                      broker === "groww" ? "Groww" :
                      broker === "5paisa" ? "5Paisa" :
                      broker === "icicidirect" ? "ICICI Direct" :
                      broker === "hdfcsky" ? "HDFC Sky" : broker
                    } Credentials
                  </h3>
                  <span className="text-[10px] font-semibold px-2 py-0.5 rounded bg-gray-100 text-gray-600 uppercase">
                    {broker}
                  </span>
                </div>

                {/* 1. Alice Blue */}
                {broker === "alicebluenew" && (
                  <div className="space-y-2.5">
                    <input
                      type="text"
                      placeholder="Alice Blue User ID"
                      value={brokerUserId}
                      onChange={(e) => setBrokerUserId(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="text"
                      placeholder="API Key"
                      value={apiKey}
                      onChange={(e) => setApiKey(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="password"
                      placeholder="API Secret / Encryption Key"
                      value={apiSecret}
                      onChange={(e) => setApiSecret(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] font-mono focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                  </div>
                )}

                {/* 2. Flattrade */}
                {broker === "flattrade" && (
                  <div className="space-y-2.5">
                    <input
                      type="text"
                      placeholder="Flattrade Client Code"
                      value={brokerUserId}
                      onChange={(e) => setBrokerUserId(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="password"
                      placeholder="Flattrade Password"
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="text"
                      placeholder="API Key"
                      value={apiKey}
                      onChange={(e) => setApiKey(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="password"
                      placeholder="API Secret"
                      value={apiSecret}
                      onChange={(e) => setApiSecret(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] font-mono focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="text"
                      placeholder="TOTP Secret Key (for auto 2FA login)"
                      value={totpKey}
                      onChange={(e) => setTotpKey(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                  </div>
                )}

                {/* 3. Zerodha */}
                {broker === "zerodha" && (
                  <div className="space-y-2.5">
                    <input
                      type="text"
                      placeholder="Kite User ID"
                      value={brokerUserId}
                      onChange={(e) => setBrokerUserId(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="text"
                      placeholder="API Key"
                      value={apiKey}
                      onChange={(e) => setApiKey(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="password"
                      placeholder="API Secret"
                      value={apiSecret}
                      onChange={(e) => setApiSecret(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] font-mono focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="text"
                      placeholder="TOTP Key (Optional)"
                      value={totpKey}
                      onChange={(e) => setTotpKey(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                  </div>
                )}

                {/* 4. Angel One */}
                {broker === "angelone" && (
                  <div className="space-y-2.5">
                    <input
                      type="text"
                      placeholder="Client Code"
                      value={brokerUserId}
                      onChange={(e) => setBrokerUserId(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="text"
                      placeholder="SmartAPI Key"
                      value={apiKey}
                      onChange={(e) => setApiKey(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="password"
                      placeholder="MPIN / Password"
                      value={apiSecret}
                      onChange={(e) => setApiSecret(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] font-mono focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="text"
                      placeholder="TOTP Secret Key"
                      value={totpKey}
                      onChange={(e) => setTotpKey(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                  </div>
                )}

                {/* 5. Dhan */}
                {broker === "dhan" && (
                  <div className="space-y-2.5">
                    <input
                      type="text"
                      placeholder="Dhan Client ID"
                      value={brokerUserId}
                      onChange={(e) => setBrokerUserId(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="password"
                      placeholder="Permanent Access Token"
                      value={apiSecret}
                      onChange={(e) => setApiSecret(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] font-mono focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                  </div>
                )}

                {/* 6. Fyers */}
                {broker === "fyers" && (
                  <div className="space-y-2.5">
                    <input
                      type="text"
                      placeholder="Fyers Client ID"
                      value={brokerUserId}
                      onChange={(e) => setBrokerUserId(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="text"
                      placeholder="App ID"
                      value={apiKey}
                      onChange={(e) => setApiKey(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="password"
                      placeholder="Secret Key / Access Token"
                      value={apiSecret}
                      onChange={(e) => setApiSecret(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] font-mono focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                  </div>
                )}

                {/* 7. Finvasia Shoonya */}
                {broker === "shoonya" && (
                  <div className="space-y-2.5">
                    <input
                      type="text"
                      placeholder="Shoonya User ID"
                      value={brokerUserId}
                      onChange={(e) => setBrokerUserId(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="text"
                      placeholder="Vendor Code / API Key"
                      value={apiKey}
                      onChange={(e) => setApiKey(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="password"
                      placeholder="Password"
                      value={apiSecret}
                      onChange={(e) => setApiSecret(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] font-mono focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="text"
                      placeholder="TOTP Secret Key"
                      value={totpKey}
                      onChange={(e) => setTotpKey(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                  </div>
                )}

                {/* 8. Upstox */}
                {broker === "upstox" && (
                  <div className="space-y-2.5">
                    <input
                      type="text"
                      placeholder="Upstox UCC ID"
                      value={brokerUserId}
                      onChange={(e) => setBrokerUserId(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="text"
                      placeholder="API Key"
                      value={apiKey}
                      onChange={(e) => setApiKey(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="password"
                      placeholder="API Secret"
                      value={apiSecret}
                      onChange={(e) => setApiSecret(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] font-mono focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                  </div>
                )}

                {/* 9. Kotak Neo */}
                {broker === "kotakneo" && (
                  <div className="space-y-2.5">
                    <input
                      type="text"
                      placeholder="Mobile No / UCC"
                      value={brokerUserId}
                      onChange={(e) => setBrokerUserId(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="text"
                      placeholder="Consumer Key"
                      value={apiKey}
                      onChange={(e) => setApiKey(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="password"
                      placeholder="Consumer Secret / Password"
                      value={apiSecret}
                      onChange={(e) => setApiSecret(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] font-mono focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                  </div>
                )}

                {/* 10. Groww */}
                {broker === "groww" && (
                  <div className="space-y-2.5">
                    <input
                      type="text"
                      placeholder="Groww User ID"
                      value={brokerUserId}
                      onChange={(e) => setBrokerUserId(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="text"
                      placeholder="API Key"
                      value={apiKey}
                      onChange={(e) => setApiKey(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="password"
                      placeholder="API Secret / Access Token"
                      value={apiSecret}
                      onChange={(e) => setApiSecret(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] font-mono focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                  </div>
                )}

                {/* 11. 5Paisa */}
                {broker === "5paisa" && (
                  <div className="space-y-2.5">
                    <input
                      type="text"
                      placeholder="5Paisa Client Code"
                      value={brokerUserId}
                      onChange={(e) => setBrokerUserId(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="text"
                      placeholder="User Key"
                      value={apiKey}
                      onChange={(e) => setApiKey(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="password"
                      placeholder="Encryption Key / Password"
                      value={apiSecret}
                      onChange={(e) => setApiSecret(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] font-mono focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                  </div>
                )}

                {/* 12. ICICI Direct */}
                {broker === "icicidirect" && (
                  <div className="space-y-2.5">
                    <input
                      type="text"
                      placeholder="ICICI Direct User ID"
                      value={brokerUserId}
                      onChange={(e) => setBrokerUserId(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="text"
                      placeholder="Breeze API Key"
                      value={apiKey}
                      onChange={(e) => setApiKey(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="password"
                      placeholder="API Secret / Session Key"
                      value={apiSecret}
                      onChange={(e) => setApiSecret(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] font-mono focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                  </div>
                )}

                {/* 13. HDFC Sky */}
                {broker === "hdfcsky" && (
                  <div className="space-y-2.5">
                    <input
                      type="text"
                      placeholder="HDFC Sky Client ID"
                      value={brokerUserId}
                      onChange={(e) => setBrokerUserId(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="text"
                      placeholder="API Key"
                      value={apiKey}
                      onChange={(e) => setApiKey(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <input
                      type="password"
                      placeholder="API Secret"
                      value={apiSecret}
                      onChange={(e) => setApiSecret(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] font-mono focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                  </div>
                )}

                {/* Divider line below API Secret / Broker credentials */}
                <div className="pt-2 border-b border-[#E5E7EB]" />
              </div>
            )}

            {/* Field 2: Nickname */}
            <div>
              <label className="block text-[11px] font-semibold text-[#4B5563] mb-1.5 uppercase tracking-wider">
                Nickname
              </label>
              <input
                type="text"
                placeholder="Enter Nickname"
                value={nickname}
                onChange={(e) => setNickname(e.target.value)}
                className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
              />
            </div>

            {/* Field 3: Mobile No. */}
            <div>
              <label className="block text-[11px] font-semibold text-[#4B5563] mb-1.5 uppercase tracking-wider">
                Mobile No.
              </label>
              <input
                type="tel"
                placeholder="Enter Mobile No."
                value={mobile}
                onChange={(e) => setMobile(e.target.value)}
                className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
              />
            </div>

            {/* Field 4: Email */}
            <div>
              <label className="block text-[11px] font-semibold text-[#4B5563] mb-1.5 uppercase tracking-wider">
                Email
              </label>
              <input
                type="email"
                placeholder="Enter Email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
              />
            </div>

            {/* Field 5: Static IP (optional - per-account proxy from any provider) */}
            <div>
              <div className="flex flex-col sm:flex-row sm:items-end gap-2.5">
                <div className="sm:w-[210px] shrink-0">
                  <label className="block text-[11px] font-semibold text-[#4B5563] mb-1.5 uppercase tracking-wider">
                    Static IP
                  </label>
                  <div className="flex items-center justify-between gap-3 h-9 px-3 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB]">
                    <span className="text-xs font-medium text-[#111111] whitespace-nowrap">Enable Static IP</span>
                    <button
                      type="button"
                      role="switch"
                      aria-checked={staticIpEnabled}
                      aria-label="Enable Static IP"
                      onClick={() => setStaticIpEnabled((v) => !v)}
                      className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none ${
                        staticIpEnabled ? "bg-[#111111]" : "bg-gray-300"
                      }`}
                    >
                      <span
                        className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${
                          staticIpEnabled ? "translate-x-4" : "translate-x-0"
                        }`}
                      />
                    </button>
                  </div>
                </div>

                {staticIpEnabled && (
                  <div className="flex-1 min-w-0">
                    <label htmlFor="static-ip-connection-type" className="block text-[11px] font-semibold text-[#4B5563] mb-1.5 uppercase tracking-wider">
                      Connection Type
                    </label>
                    <OptionDropdown
                      id="static-ip-connection-type"
                      ariaLabel="Static IP connection type"
                      value={connectionType}
                      onChange={setConnectionType}
                      options={CONNECTION_TYPES}
                      fullWidth
                      align="left"
                      selectedTextClass="text-[#111111]"
                      optionTextClass="text-[#111111]"
                    />
                  </div>
                )}
              </div>

              {staticIpEnabled && (
                <div className="mt-2.5 space-y-2.5 animate-in fade-in duration-200">
                  {connectionType === "host" ? (
                    <>
                      <div>
                        <label className="block text-[10px] font-semibold text-[#4B5563] mb-1 uppercase tracking-wider">
                          Static IP / Host
                        </label>
                        <input
                          type="text"
                          placeholder="Proxy Host"
                          value={proxyHost}
                          onChange={(e) => setProxyHost(e.target.value)}
                          className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111] font-mono"
                        />
                      </div>
                      <div>
                        <label className="block text-[10px] font-semibold text-[#4B5563] mb-1 uppercase tracking-wider">
                          Port
                        </label>
                        <input
                          type="text"
                          inputMode="numeric"
                          placeholder="Port"
                          value={proxyPort}
                          onChange={(e) => setProxyPort(e.target.value)}
                          className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111] font-mono"
                        />
                      </div>
                      <div>
                        <label className="block text-[10px] font-semibold text-[#4B5563] mb-1 uppercase tracking-wider">
                          Username
                        </label>
                        <input
                          type="text"
                          placeholder="Provider username"
                          value={proxyUsername}
                          onChange={(e) => setProxyUsername(e.target.value)}
                          className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                        />
                      </div>
                      <div>
                        <label className="block text-[10px] font-semibold text-[#4B5563] mb-1 uppercase tracking-wider">
                          Password
                        </label>
                        <input
                          type="password"
                          placeholder="••••••••••••"
                          value={proxyPassword}
                          onChange={(e) => setProxyPassword(e.target.value)}
                          className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111] font-mono"
                        />
                      </div>
                    </>
                  ) : (
                    <div>
                      <label className="block text-[10px] font-semibold text-[#4B5563] mb-1 uppercase tracking-wider">
                        Proxy URL
                      </label>
                      <input
                        type="password"
                        autoComplete="off"
                        placeholder="https://username:password@host:port"
                        value={proxyUrlInput}
                        onChange={(e) => setProxyUrlInput(e.target.value)}
                        className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111] font-mono"
                      />
                      <p className="mt-1 text-[10px] text-[#6B7280]">
                        Include http:// or https://. The username and password stay hidden while you type.
                      </p>
                    </div>
                  )}

                  <div>
                    <label
                      htmlFor="assigned-public-ip"
                      className="block text-[10px] font-semibold text-[#4B5563] mb-1 uppercase tracking-wider"
                    >
                      Assigned Public IP
                    </label>
                    <input
                      id="assigned-public-ip"
                      type="text"
                      value={assignedIp}
                      onChange={(e) => setAssignedIp(e.target.value)}
                      placeholder="Assigned Public IP"
                      autoComplete="off"
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
                    />
                    <p className="mt-1 text-[10px] text-[#6B7280]">
                      The IP shown for this plan in your StaticIP.in dashboard. Used only to count distinct Static IPs;
                      it does not change routing.
                    </p>
                  </div>

                  <div>
                    <label className="block text-[10px] font-semibold text-[#4B5563] mb-1 uppercase tracking-wider">
                      Expiry Date
                    </label>
                    <DatePicker
                      value={proxyExpiry}
                      onChange={setProxyExpiry}
                      ariaLabel="Static IP expiry date"
                      placeholder="Select date"
                    />
                    <p className="mt-1 text-[10px] text-[#6B7280]">
                      Last day the IP is valid. Leave empty if you do not track expiry.
                    </p>
                  </div>

                  <p className="text-[10px] text-amber-700">
                    ⚠ API calls for this account will be routed via this Static IP.
                  </p>
                </div>
              )}
            </div>

          </form>
        </div>

        {/* Fixed Bottom Action Bar */}
        <div className="px-6 py-4 pb-5 bg-white border-t border-[#E5E7EB] flex items-center justify-end gap-2.5 shrink-0 box-border">
          {accountToEdit ? (
            <button
              type="button"
              onClick={() => void submit("save")}
              disabled={submitting}
              className="inline-flex items-center justify-center gap-1.5 h-8 px-3.5 rounded-lg border text-xs font-semibold shadow-sm transition-all active:scale-[0.98] bg-[#111111] hover:bg-[#1F2937] text-white disabled:opacity-50 border-[#111111]"
            >
              {submitting && submitMode === "save" ? "Saving..." : "Save"}
            </button>
          ) : (
            <button
              type="button"
              onClick={onClose}
              className="inline-flex items-center justify-center gap-1.5 h-8 px-3.5 rounded-lg border text-xs font-semibold shadow-sm transition-all active:scale-[0.98] bg-red-600 hover:bg-red-700 text-white border-red-600 hover:border-red-700"
            >
              Close
            </button>
          )}
          <button
            type="submit"
            form="add-demat-form"
            disabled={submitting}
            className="inline-flex items-center justify-center gap-1.5 h-8 px-3.5 rounded-lg border text-xs font-semibold shadow-sm transition-all active:scale-[0.98] bg-jettone-orange hover:bg-jettone-orange-hover text-white disabled:opacity-50 border-jettone-orange hover:border-jettone-orange-hover"
          >
            {submitting && (submitMode === "reconnect" || !accountToEdit)
              ? accountToEdit
                ? "Connecting..."
                : "Adding..."
              : accountToEdit
              ? "Save & Reconnect"
              : "Add"}
          </button>
        </div>
      </div>
    </div>,
    document.body
  );
}
