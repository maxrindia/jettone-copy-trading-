import { describe, expect, it } from "vitest";
import { formatInr, moneyTone, moneyToneClass } from "./money";

describe("formatInr", () => {
  it("formats a profit with an explicit plus sign", () => {
    expect(formatInr("1.52")).toBe("+₹1.52");
  });

  it("formats a loss with a true minus sign (U+2212), not a hyphen", () => {
    expect(formatInr("-1.52")).toBe("−₹1.52");
    expect(formatInr("-1.48")).not.toContain("-");
  });

  it("formats zero without a sign, including a negative zero", () => {
    expect(formatInr("0.00")).toBe("₹0.00");
    expect(formatInr("-0.00")).toBe("₹0.00");
  });

  it("shows ₹0.00 when there is no verified value", () => {
    expect(formatInr(null)).toBe("₹0.00");
    expect(formatInr(undefined)).toBe("₹0.00");
    expect(formatInr("")).toBe("₹0.00");
    expect(formatInr("abc")).toBe("₹0.00");
  });

  it("groups digits the Indian way", () => {
    expect(formatInr("4200.16")).toBe("+₹4,200.16");
    expect(formatInr("-123456.78")).toBe("−₹1,23,456.78");
    expect(formatInr("10000000.00")).toBe("+₹1,00,00,000.00");
  });

  it("never pushes the amount through a float", () => {
    // 9007199254740993 is not representable as a double (it would print ...992).
    expect(formatInr("9007199254740993.00")).toBe("+₹9,00,71,99,25,47,40,993.00");
    expect(formatInr("0.10")).toBe("+₹0.10");
  });
});

describe("moneyTone / moneyToneClass", () => {
  it("is positive, negative, or neutral from the value's own sign", () => {
    expect(moneyTone("1.52")).toBe("positive");
    expect(moneyTone("-1.52")).toBe("negative");
    expect(moneyTone("0.00")).toBe("neutral");
    expect(moneyTone(null)).toBe("neutral");
  });

  it("maps green / red / neutral", () => {
    expect(moneyToneClass("1.52")).toBe("text-green-600");
    expect(moneyToneClass("-1.52")).toBe("text-red-600");
    expect(moneyToneClass("0.00")).toBe("text-[#6B7280]");
    expect(moneyToneClass(null)).toBe("text-[#6B7280]");
  });
});
