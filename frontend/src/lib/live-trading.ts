/**
 * Live Trading ON/OFF - the owner's runtime switch (backend:
 * GET/PUT /reliability/live-trading). Pure helpers, kept free of React so they
 * can be unit tested.
 */

export interface LiveTradingState {
  enabled: boolean;
  /** "runtime" = set from the app, "env_default" = nothing stored yet, "error" = unreadable (treated as OFF). */
  source: "runtime" | "env_default" | "error" | string;
  updated_at: string | null;
  updated_by: string | null;
  master_trading_enabled: boolean;
  emergency_stop_active: boolean;
}

export interface LiveTradingEnvelope {
  success?: boolean;
  data?: LiveTradingState | null;
}

export type LiveTradingTone = "on" | "off" | "paused" | "unknown";

export interface LiveTradingBadge {
  tone: LiveTradingTone;
  label: string;
  /** Why ON is not actually copying (master switch OFF or emergency stop), if so. */
  note: string | null;
}

export function unwrapLiveTrading(body: LiveTradingEnvelope | LiveTradingState | null | undefined): LiveTradingState | null {
  if (!body) return null;
  if ("enabled" in body) return body as LiveTradingState;
  return (body as LiveTradingEnvelope).data ?? null;
}

export function liveTradingBadge(state: LiveTradingState | null): LiveTradingBadge {
  if (!state) return { tone: "unknown", label: "Live trading: …", note: null };
  if (state.source === "error") {
    return { tone: "off", label: "Live trading: OFF", note: "The state could not be read, so trading is treated as OFF." };
  }
  if (!state.enabled) return { tone: "off", label: "Live trading: OFF", note: null };
  if (state.emergency_stop_active) {
    return { tone: "paused", label: "Live trading: ON", note: "Paused: the emergency stop is active." };
  }
  if (!state.master_trading_enabled) {
    return { tone: "paused", label: "Live trading: ON", note: "Paused: the Master trading switch is OFF." };
  }
  return { tone: "on", label: "Live trading: ON", note: null };
}

/** Body for PUT. Turning ON is always an explicit, confirmed request. */
export function liveTradingRequest(enable: boolean, reason?: string): { enabled: boolean; confirm: boolean; reason: string } {
  return {
    enabled: enable,
    confirm: enable,
    reason: reason?.trim() || (enable ? "Turned ON from the app" : "Turned OFF from the app"),
  };
}
