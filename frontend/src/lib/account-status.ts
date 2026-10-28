/**
 * How the UI describes an account's broker, connection and copy-trading roles. It only DESCRIBES what the engine reports on each
 * account (`connection_status`, `connection_detail`, `broker_capabilities`); it never decides that an account is connected or that a
 * role is allowed. The engine enforces every gate itself.
 *
 * Fail closed: a response that lacks these fields (an older engine) shows the account as not connected and every role as unavailable,
 * never as connected or allowed.
 *
 * Setting an account up is separate from copy trading with it: "Setup" says whether the broker can be signed in; "Master" / "Child"
 * say whether it may take part in copying, each with the engine's reason when blocked.
 */
export type ConnectionStatus =
  | "CONNECTED"
  | "DISCONNECTED"
  | "STATUS_UNKNOWN"
  | "NEEDS_CREDENTIALS"
  | "BROKER_NOT_ENABLED"
  | "UNSUPPORTED_BROKER";

export interface ServerBrokerCapabilities {
  broker_id: string | null;
  support: string;
  login: boolean;
  as_master: boolean;
  as_child: boolean;
  master_blocker: string | null;
  child_blocker: string | null;
  note: string;
  /** How far a successful login confirms WHICH broker account it was (CONFIRMED / CREDENTIAL_BOUND / NOT_AVAILABLE). Informational only. */
  identity?: { level: string; note: string } | null;
}

export interface AccountStatusFields {
  broker?: string | null;
  session_valid: boolean;
  connection_status?: string | null;
  connection_detail?: string | null;
  broker_capabilities?: ServerBrokerCapabilities | null;
  credentials_configured?: boolean | null;
  /** False = the engine could not check this account's session, so its connection state is unknown. */
  session_store_available?: boolean | null;
}

export type Tone = "ok" | "warn" | "muted";

export interface ConnectionView {
  status: ConnectionStatus;
  label: string;
  tone: Tone;
  detail: string;
}

const KNOWN: readonly ConnectionStatus[] = ["CONNECTED", "DISCONNECTED", "STATUS_UNKNOWN", "NEEDS_CREDENTIALS", "BROKER_NOT_ENABLED", "UNSUPPORTED_BROKER"];

const LABELS: Readonly<Record<ConnectionStatus, { label: string; tone: Tone }>> = {
  CONNECTED: { label: "Connected", tone: "ok" },
  DISCONNECTED: { label: "Not signed in", tone: "muted" },
  STATUS_UNKNOWN: { label: "Status unknown", tone: "warn" },
  NEEDS_CREDENTIALS: { label: "Needs credentials", tone: "warn" },
  BROKER_NOT_ENABLED: { label: "Broker not enabled", tone: "warn" },
  UNSUPPORTED_BROKER: { label: "Unsupported broker", tone: "warn" },
};

export const NOT_REPORTED = "The engine did not report this broker's capabilities.";

export function connectionView(acc: AccountStatusFields): ConnectionView {
  const reported = acc.connection_status as ConnectionStatus | null | undefined;
  if (reported && KNOWN.includes(reported)) {
    return { status: reported, ...LABELS[reported], detail: acc.connection_detail || "" };
  }
  // An engine that does not report a state: trust only the session flag, and only for "not connected".
  return { status: "DISCONNECTED", ...LABELS.DISCONNECTED, detail: acc.connection_detail || "" };
}

export interface RoleChip {
  role: "Master" | "Child";
  available: boolean;
  reason: string | null;
}

/** Master and Child availability exactly as the server reports it, with the server's reason for each blocked role. */
export function roleChips(caps: ServerBrokerCapabilities | null | undefined): RoleChip[] {
  if (!caps) {
    return [
      { role: "Master", available: false, reason: NOT_REPORTED },
      { role: "Child", available: false, reason: NOT_REPORTED },
    ];
  }
  return [
    { role: "Master", available: caps.as_master === true, reason: caps.as_master === true ? null : caps.master_blocker || NOT_REPORTED },
    { role: "Child", available: caps.as_child === true, reason: caps.as_child === true ? null : caps.child_blocker || NOT_REPORTED },
  ];
}

/**
 * The engine's note when an account is connected but its broker gives Jettone no way to confirm WHICH broker account the login was
 * (so the account name entered here is a label, not a proven identity). Null for a connected account whose identity the engine
 * can confirm or tie to the login, and for any account that is not connected. It never affects a Master / Child role.
 */
export function identityNotice(acc: AccountStatusFields): string | null {
  if (connectionView(acc).status !== "CONNECTED") return null;
  const identity = acc.broker_capabilities?.identity;
  return identity && identity.level === "NOT_AVAILABLE" ? identity.note || "Jettone cannot confirm which broker account this login belongs to." : null;
}

/** Whether the broker can be signed in at all (setup), independent of copy trading. */
export function canSetUp(caps: ServerBrokerCapabilities | null | undefined): boolean {
  return caps?.login === true;
}
