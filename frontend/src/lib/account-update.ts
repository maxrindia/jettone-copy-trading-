/**
 * Submit orchestration for the Demat account dialog ("Save" / "Save &
 * Reconnect"), kept free of React so the ordering and failure rules can be
 * unit tested.
 */

export interface AccountUpdateFields {
  displayName: string;
  broker: string;
  notes: string;
  proxyExpiresOn: string | null;
  staticIpAddress: string | null;
}

/**
 * Body of PUT /api/v1/accounts/{id}: the edited details only. `is_active` is
 * never sent - on this platform it is the account's Trading switch, and
 * neither Save nor Save & Reconnect may turn trading on or off.
 */
export function buildAccountUpdateBody(f: AccountUpdateFields): Record<string, unknown> {
  return {
    display_name: f.displayName,
    broker: f.broker,
    notes: f.notes,
    proxy_expires_on: f.proxyExpiresOn,
    static_ip_address: f.staticIpAddress,
  };
}

/**
 * The broker a dialog opened on an existing account should show. The ACCOUNT's own stored broker wins; the "Broker:" line in its
 * notes (which only dialogs written by this app add) is a fallback for older rows, and Alice Blue is the last resort. Reading the
 * notes alone made a Groww / Angel One / Zerodha account without that line open as Alice Blue, and saving it was then refused.
 */
export function brokerForEdit(account: { broker?: string | null; notes?: string | null }): string {
  const stored = (account.broker || "").trim().toLowerCase();
  if (stored) return stored;
  const fromNotes = account.notes?.match(/Broker:\s*([^\s,]+)/i)?.[1];
  return (fromNotes || "alicebluenew").trim().toLowerCase();
}

/**
 * Which account step 1 of the dialog should write to. An account being edited, or one this dialog already created in an earlier
 * attempt (its credentials or login then failed), is UPDATED; creating it again would only hit "already exists". Only a dialog
 * with neither creates a new account.
 */
export function saveTargetId(editingId: string | null | undefined, createdId: string | null | undefined): string | null {
  return editingId || createdId || null;
}

export type SubmitMode = "save" | "reconnect";

export type SubmitOutcome =
  | { status: "saved" }
  | { status: "reconnected" }
  | { status: "save_failed"; message: string }
  | { status: "reconnect_failed"; message: string };

function messageOf(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

/**
 * "save": persist the details and stop.
 * "reconnect": persist the details, and only after that succeeded start the
 * reconnect/login flow with what the save returned. A failed save never
 * reaches the reconnect step; a failed reconnect is reported as such (the
 * details are already saved by then).
 */
export async function runSubmit<T>(
  mode: SubmitMode,
  save: () => Promise<T>,
  reconnect: (saved: T) => Promise<void>,
): Promise<SubmitOutcome> {
  let saved: T;
  try {
    saved = await save();
  } catch (err) {
    return { status: "save_failed", message: messageOf(err, "The account details could not be saved.") };
  }
  if (mode === "save") return { status: "saved" };

  try {
    await reconnect(saved);
  } catch (err) {
    return { status: "reconnect_failed", message: messageOf(err, "The reconnect could not be started.") };
  }
  return { status: "reconnected" };
}

/**
 * Re-entry guard that works synchronously (React state updates are async, so a
 * fast double click could otherwise start two submissions, and with them two
 * saves or two reconnect intents).
 */
export function createSubmitGuard(): { tryEnter: () => boolean; leave: () => void; readonly busy: boolean } {
  let busy = false;
  return {
    tryEnter() {
      if (busy) return false;
      busy = true;
      return true;
    },
    leave() {
      busy = false;
    },
    get busy() {
      return busy;
    },
  };
}
