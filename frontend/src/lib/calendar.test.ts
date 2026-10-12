import { describe, expect, it } from "vitest";
import { monthGrid, monthLabel, parseIso, shiftMonth, toIso, todayIso } from "./calendar";

describe("parseIso / toIso", () => {
  it("round-trips real dates and rejects impossible ones", () => {
    expect(parseIso("2026-10-15")).toEqual({ year: 2026, month: 9, day: 15 });
    expect(toIso(2026, 9, 5)).toBe("2026-10-05");
    for (const v of ["2026-02-30", "2027-02-29", "2026-13-01", "15-10-2026", "", null, undefined]) {
      expect(parseIso(v as string | null | undefined)).toBeNull();
    }
    expect(parseIso("2028-02-29")).not.toBeNull();
  });
});

describe("shiftMonth", () => {
  it("crosses year boundaries in both directions", () => {
    expect(shiftMonth({ year: 2026, month: 11 }, 1)).toEqual({ year: 2027, month: 0 });
    expect(shiftMonth({ year: 2026, month: 0 }, -1)).toEqual({ year: 2025, month: 11 });
    expect(shiftMonth({ year: 2026, month: 5 }, 14)).toEqual({ year: 2027, month: 7 });
    expect(shiftMonth({ year: 2026, month: 5 }, -30)).toEqual({ year: 2023, month: 11 });
  });
});

describe("monthGrid", () => {
  it("lays October 2026 out Sunday-first (1st is a Thursday)", () => {
    const weeks = monthGrid({ year: 2026, month: 9 });
    expect(weeks[0]).toEqual([null, null, null, null, "2026-10-01", "2026-10-02", "2026-10-03"]);
    expect(weeks[weeks.length - 1].filter(Boolean).pop()).toBe("2026-10-31");
    expect(weeks.every((w) => w.length === 7)).toBe(true);
    expect(weeks.flat().filter(Boolean)).toHaveLength(31);
  });
  it("handles leap February and a month starting on Sunday", () => {
    expect(monthGrid({ year: 2028, month: 1 }).flat().filter(Boolean)).toHaveLength(29);
    expect(monthGrid({ year: 2026, month: 1 })[0][0]).toBe("2026-02-01"); // Feb 2026 starts on a Sunday
  });
});

describe("labels and today", () => {
  it("names the month", () => {
    expect(monthLabel({ year: 2026, month: 9 })).toBe("October 2026");
  });
  it("todayIso is a valid YYYY-MM-DD", () => {
    expect(parseIso(todayIso())).not.toBeNull();
  });
});
