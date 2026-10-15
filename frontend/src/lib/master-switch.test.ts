import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { CNC_ACKNOWLEDGEMENT_TEXT, canConfirmSwitch, requiresCncAcknowledgement } from "./master-switch";

const base = { checking: false, unverifiedAcknowledged: false, cncAcknowledged: false };
const ready = { allowed: true, warnings: [] };
const withCnc = { allowed: true, requires_cnc_acknowledgement: true, warnings: [{ code: "CNC_POSITIONS_OPEN" }] };

describe("canConfirmSwitch", () => {
  it("blocks CNC positions until the acknowledgement is checked", () => {
    expect(canConfirmSwitch({ ...base, preflight: withCnc })).toBe(false);
    expect(canConfirmSwitch({ ...base, preflight: withCnc, cncAcknowledged: true })).toBe(true);
  });

  it("does not need an acknowledgement for intraday-only positions", () => {
    const intraday = { allowed: true, warnings: [{ code: "INTRADAY_POSITIONS_OPEN" }] };
    expect(requiresCncAcknowledgement(intraday)).toBe(false);
    expect(canConfirmSwitch({ ...base, preflight: intraday })).toBe(true);
  });

  it("is unchanged when there are no positions", () => {
    expect(canConfirmSwitch({ ...base, preflight: ready })).toBe(true);
  });

  it("never enables the button before a preflight, or while checking", () => {
    expect(canConfirmSwitch({ ...base, preflight: null })).toBe(false);
    expect(canConfirmSwitch({ ...base, preflight: ready, checking: true })).toBe(false);
  });

  it("keeps real blockers blocking, even with every box ticked", () => {
    const blocked = { allowed: false, can_override: false, warnings: [{ code: "CNC_POSITIONS_OPEN" }] };
    expect(canConfirmSwitch({ ...base, preflight: blocked, cncAcknowledged: true, unverifiedAcknowledged: true })).toBe(false);
  });

  it("keeps the existing 'broker unreadable' override flow", () => {
    const unreadable = { allowed: false, can_override: true, warnings: [] };
    expect(canConfirmSwitch({ ...base, preflight: unreadable })).toBe(false);
    expect(canConfirmSwitch({ ...base, preflight: unreadable, unverifiedAcknowledged: true })).toBe(true);
  });

  it("detects CNC from the warning list even without the flag", () => {
    expect(requiresCncAcknowledgement({ allowed: true, warnings: [{ code: "CNC_POSITIONS_OPEN" }] })).toBe(true);
    expect(requiresCncAcknowledgement(null)).toBe(false);
  });
});

describe("acknowledgement text and reset behaviour", () => {
  it("uses the exact requested wording", () => {
    expect(CNC_ACKNOWLEDGEMENT_TEXT).toBe(
      "I acknowledge that these CNC positions will remain open in Alice Blue. Jettone will not close or transfer them automatically.",
    );
  });

  const dialog = readFileSync(join(__dirname, "../components/strategy/MasterSwitchDialog.tsx"), "utf8");
  const page = readFileSync(join(__dirname, "../app/(app)/group-strategy/[groupId]/page.tsx"), "utf8");

  it("starts unchecked, resets on every re-check, and is sent to the engine", () => {
    expect(dialog).toMatch(/useState\(false\);\s*\n\s*\n?\s*\/\/ keepError|const \[cncAcknowledged, setCncAcknowledged\] = useState\(false\)/);
    expect(dialog).toMatch(/setPreflight\(body as SwitchPreflight\);\s*setAcknowledged\(false\);\s*setCncAcknowledged\(false\);/);
    expect(dialog).toContain("acknowledge_cnc_positions: cncAcknowledged");
  });

  it("is unmounted when closed, so a reopened dialog starts unchecked", () => {
    expect(page).toMatch(/\{mounted && pendingMaster && \(\s*<MasterSwitchDialog/);
    expect(page).toContain("onClose={() => setPendingMasterId(null)}");
  });
});
