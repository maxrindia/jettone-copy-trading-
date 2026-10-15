/**
 * Master-switch dialog rules, kept pure so they can be unit tested.
 *
 * The engine is the authority: it re-runs the preflight under its locks and
 * refuses a switch that needs the CNC acknowledgement when the request does
 * not carry it. The UI only mirrors that so the user can see why a button is
 * disabled.
 */

export const CNC_ACKNOWLEDGEMENT_TEXT =
  "I acknowledge that these CNC positions will remain open in Alice Blue. Jettone will not close or transfer them automatically.";

export interface SwitchWarningLike {
  code: string;
}

export interface SwitchPreflightLike {
  allowed: boolean;
  can_override?: boolean;
  requires_cnc_acknowledgement?: boolean;
  warnings: SwitchWarningLike[];
}

/** CNC warnings are what the acknowledgement covers. */
export function requiresCncAcknowledgement(pre: SwitchPreflightLike | null): boolean {
  if (!pre) return false;
  return pre.requires_cnc_acknowledgement === true || pre.warnings.some((w) => w.code === "CNC_POSITIONS_OPEN");
}

export interface ConfirmState {
  preflight: SwitchPreflightLike | null;
  checking: boolean;
  /** "Alice Blue could not be read" acknowledgement (existing override flow). */
  unverifiedAcknowledged: boolean;
  /** CNC positions acknowledgement. */
  cncAcknowledged: boolean;
}

/**
 * Whether "Change Master" is enabled. Nothing is allowed until a preflight
 * exists; a blocked preflight is only overridable in the existing
 * "broker unreadable" case; CNC positions additionally need the checkbox.
 */
export function canConfirmSwitch(s: ConfirmState): boolean {
  const pre = s.preflight;
  if (!pre || s.checking) return false;
  if (pre.allowed) {
    return !requiresCncAcknowledgement(pre) || s.cncAcknowledged;
  }
  return pre.can_override === true && s.unverifiedAcknowledged;
}
