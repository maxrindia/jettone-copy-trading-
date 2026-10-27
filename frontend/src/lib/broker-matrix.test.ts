import { describe, expect, it } from "vitest";
import {
  MATRIX_NOT_LOADED,
  brokerLabel,
  masterDetectionBlocker,
  matrixRows,
  roleSummary,
  type BrokerMatrixData,
  type MatrixBroker,
  type MatrixRole,
} from "./broker-matrix";

const role = (over: Partial<MatrixRole> = {}): MatrixRole => ({
  gate_open: false, evidence: "UNVERIFIED", missing_for_design_bar: [], registry_reason: "", ...over,
});
const broker = (id: string, over: Partial<MatrixBroker> = {}): MatrixBroker => ({
  id, support: "PARTIAL", master: role(), child: role(), master_detection: null, ...over,
});
const ids = ["alicebluenew", "groww"];
const data = (): BrokerMatrixData => ({
  brokers: ids.map((i) => broker(i)),
  matrix: ids.flatMap((m) =>
    ids.map((c) => ({
      master: m, child: c, supported: m === "alicebluenew" && c === "alicebluenew", requirement: null, detail: "PAIR_NOT_ALLOWLISTED",
      message: m === "alicebluenew" && c === "alicebluenew" ? "ok" : "BROKER_PAIR_NOT_SUPPORTED: not allowlisted",
    })),
  ),
  supported: [],
  note: "",
});

describe("matrixRows", () => {
  it("builds one row per master with a cell per child and only the allowlisted pair supported", () => {
    const rows = matrixRows(data());
    expect(rows.map((r) => r.master)).toEqual(ids);
    expect(rows[0].cells.map((c) => c.supported)).toEqual([true, false]);
    expect(rows[1].cells.every((c) => !c.supported)).toBe(true);
    expect(rows[0].cells[0].reason).toBe("Supported");
    expect(rows[0].cells[1].reason).toContain("BROKER_PAIR_NOT_SUPPORTED");
  });

  it("fails closed: no data, bad shape or a missing cell is never supported", () => {
    expect(matrixRows(null)).toEqual([]);
    expect(matrixRows({ brokers: "x", matrix: [] } as unknown as BrokerMatrixData)).toEqual([]);
    const d = data();
    d.matrix = d.matrix.filter((c) => !(c.master === "groww" && c.child === "groww"));
    const cell = matrixRows(d)[1].cells[1];
    expect(cell.supported).toBe(false);
    expect(cell.reason).toBe(MATRIX_NOT_LOADED);
  });

  it("treats only a literal true as supported", () => {
    const d = data();
    (d.matrix[1] as unknown as { supported: unknown }).supported = "true";
    expect(matrixRows(d)[0].cells[1].supported).toBe(false);
  });
});

describe("labels and summaries", () => {
  it("names the four brokers and falls back to the id", () => {
    expect(["alicebluenew", "groww", "angelone", "zerodha"].map(brokerLabel)).toEqual(["Alice Blue", "Groww", "Angel One", "Zerodha"]);
    expect(brokerLabel("dhan")).toBe("dhan");
  });

  it("summarises a role", () => {
    expect(roleSummary(undefined)).toBe("Not available");
    expect(roleSummary(role())).toBe("Closed");
    expect(roleSummary(role({ gate_open: true }))).toBe("Open");
    expect(roleSummary(role({ gate_open: true, evidence: "VERIFIED_DOCS", missing_for_design_bar: ["MODIFY"] }))).toBe("Open, verified docs");
  });

  it("reports the recorded master-detection blocker only when unverified", () => {
    expect(masterDetectionBlocker(undefined)).toBeNull();
    expect(masterDetectionBlocker(broker("a"))).toBeNull();
    expect(masterDetectionBlocker(broker("a", { master_detection: { mode: "POLL", external_orders_verified: true, blocker: null } }))).toBeNull();
    expect(masterDetectionBlocker(broker("z", { master_detection: { mode: "POLL", external_orders_verified: false, blocker: "silent docs" } }))).toBe("silent docs");
    expect(masterDetectionBlocker(broker("z", { master_detection: { mode: "POLL", external_orders_verified: false, blocker: null } }))).toBe(
      "Master order detection is not verified.",
    );
  });
});
