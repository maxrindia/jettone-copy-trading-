/**
 * View model for GET /api/v1/brokers/matrix: the 4 x 4 Master -> Child compatibility matrix and each broker's role evidence.
 * It only DESCRIBES the engine's decision so the UI can show what is blocked and why; the engine enforces every gate itself
 * (membership, activation, the Master switch and order time), so nothing here grants or enables anything.
 *
 * Fail closed: until the matrix has loaded, every pair is reported as blocked with "not loaded".
 */
export interface MatrixRole {
  gate_open: boolean;
  evidence: string;
  missing_for_design_bar: string[];
  registry_reason: string;
}

export interface MatrixBroker {
  id: string;
  support: string;
  master: MatrixRole;
  child: MatrixRole;
  master_detection: { mode: string; external_orders_verified: boolean; blocker: string | null } | null;
}

export interface MatrixCell {
  master: string;
  child: string;
  supported: boolean;
  requirement: string | null;
  detail: string;
  message: string;
  /** Every requirement still missing for real trading (not just the first). Optional: older engines do not send it. */
  missing?: { requirement: string; detail: string }[];
  /** The engine path is implemented and exercised with fakes. This is not broker verification. */
  code_complete?: boolean;
  real_broker_verified?: boolean;
}

export interface BrokerMatrixData {
  brokers: MatrixBroker[];
  matrix: MatrixCell[];
  supported: MatrixCell[];
  note: string;
}

export const BROKER_LABELS: Readonly<Record<string, string>> = {
  alicebluenew: "Alice Blue",
  groww: "Groww",
  angelone: "Angel One",
  zerodha: "Zerodha",
};

export const MATRIX_NOT_LOADED = "The engine's broker compatibility has not loaded, so no pair is shown as supported.";

export function brokerLabel(id: string): string {
  return BROKER_LABELS[id] ?? id;
}

export interface MatrixRow {
  master: string;
  cells: { child: string; supported: boolean; reason: string }[];
}

/** The engine's reason plus every missing requirement. "Code complete" never means verified with the real broker. */
export function blockedReason(cell: MatrixCell): string {
  const base = cell.message || cell.detail;
  const missing = Array.isArray(cell.missing) ? cell.missing.map((m) => m.detail).filter(Boolean) : [];
  const parts = [base];
  if (missing.length > 0) parts.push(`Missing: ${missing.join("; ")}.`);
  if (cell.code_complete === true && cell.real_broker_verified !== true) {
    parts.push("The copy path is implemented and tested with simulated brokers; it is not verified with the real broker.");
  }
  return parts.join(" ");
}

/** One row per Master broker, in the engine's broker order. A missing cell is blocked, never supported. */
export function matrixRows(data: BrokerMatrixData | null | undefined): MatrixRow[] {
  if (!data || !Array.isArray(data.brokers) || !Array.isArray(data.matrix)) return [];
  const ids = data.brokers.map((b) => b.id);
  return ids.map((master) => ({
    master,
    cells: ids.map((child) => {
      const cell = data.matrix.find((c) => c.master === master && c.child === child);
      if (!cell) return { child, supported: false, reason: MATRIX_NOT_LOADED };
      return { child, supported: cell.supported === true, reason: cell.supported === true ? "Supported" : blockedReason(cell) };
    }),
  }));
}

/** Plain-language status of one role, for the per-broker list. */
export function roleSummary(role: MatrixRole | undefined): string {
  if (!role) return "Not available";
  if (!role.gate_open) return "Closed";
  return role.missing_for_design_bar.length > 0 ? `Open, ${role.evidence.toLowerCase().replace(/_/g, " ")}` : "Open";
}

/** The exact unverified fact that keeps a broker from being a Master, or null when it is verified or unknown. */
export function masterDetectionBlocker(broker: MatrixBroker | undefined): string | null {
  const d = broker?.master_detection;
  if (!d || d.external_orders_verified) return null;
  return d.blocker || "Master order detection is not verified.";
}
