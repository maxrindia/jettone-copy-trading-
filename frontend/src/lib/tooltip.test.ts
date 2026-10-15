import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import Tooltip, { TOOLTIP_SURFACE_CLASS } from "../components/ui/Tooltip";
import { quantityPolicyLabel } from "../components/strategy/QuantityPolicyDialog";
import { computeTooltipPosition } from "./tooltip";

const viewport = { w: 1000, h: 700 };
const tip = { w: 200, h: 40 };
const anchorAt = (left: number, top: number, w = 80, h = 24) => ({ left, top, right: left + w, bottom: top + h });

describe("computeTooltipPosition", () => {
  it("sits above the anchor, centred, with the arrow under the anchor centre", () => {
    const p = computeTooltipPosition({ anchor: anchorAt(400, 300), tip, viewport });
    expect(p.side).toBe("top");
    expect(p.left).toBe(440 - 100); // anchor centre 440, tip 200 wide
    expect(p.top).toBe(300 - 8 - 40);
    expect(p.arrowLeft).toBe(100);
  });

  it("flips below when there is no room above", () => {
    const p = computeTooltipPosition({ anchor: anchorAt(400, 10), tip, viewport });
    expect(p.side).toBe("bottom");
    expect(p.top).toBe(10 + 24 + 8);
  });

  it("flips above when preferring below near the bottom edge", () => {
    const p = computeTooltipPosition({ anchor: anchorAt(400, 660), tip, viewport, prefer: "bottom" });
    expect(p.side).toBe("top");
  });

  it("is clamped inside the viewport at the left and right edges, and the arrow still points at the anchor", () => {
    const left = computeTooltipPosition({ anchor: anchorAt(0, 300, 20), tip, viewport });
    expect(left.left).toBe(8);
    expect(left.arrowLeft).toBeGreaterThanOrEqual(10);
    expect(left.arrowLeft).toBeLessThan(tip.w);

    const right = computeTooltipPosition({ anchor: anchorAt(980, 300, 20), tip, viewport });
    expect(right.left).toBe(1000 - 200 - 8);
    expect(right.arrowLeft).toBeLessThanOrEqual(tip.w - 10);
  });

  it("stays on screen on a narrow mobile viewport", () => {
    const p = computeTooltipPosition({ anchor: anchorAt(150, 300, 40), tip: { w: 240, h: 60 }, viewport: { w: 320, h: 568 } });
    expect(p.left).toBeGreaterThanOrEqual(8);
    expect(p.left + 240).toBeLessThanOrEqual(320 - 8);
  });

  it("picks the roomier side when neither fits", () => {
    const p = computeTooltipPosition({ anchor: anchorAt(400, 20, 80, 20), tip: { w: 100, h: 90 }, viewport: { w: 1000, h: 100 } });
    expect(["top", "bottom"]).toContain(p.side);
    expect(p.top).toBeGreaterThanOrEqual(8);
  });
});

describe("Tooltip component", () => {
  const trigger = createElement("button", { type: "button" }, "Pend");

  it("renders children untouched when there is no help text", () => {
    expect(renderToStaticMarkup(createElement(Tooltip, { content: undefined, children: trigger }))).toBe("<button type=\"button\">Pend</button>");
    expect(renderToStaticMarkup(createElement(Tooltip, { content: "", children: trigger }))).toBe("<button type=\"button\">Pend</button>");
  });

  it("wraps without changing layout, uses no native title, and is closed until hovered or focused", () => {
    const html = renderToStaticMarkup(createElement(Tooltip, { content: "Pending: orders still working", children: trigger }));
    expect(html).toContain("contents");
    expect(html).not.toContain("title=");
    expect(html).not.toContain("role=\"tooltip\"");
    expect(html).not.toContain("Pending: orders still working"); // portal content only exists while open
  });

  it("makes non-interactive help targets keyboard reachable when asked", () => {
    const html = renderToStaticMarkup(createElement(Tooltip, { content: "Help", focusable: true, children: createElement("span", null, "1.00x") }));
    expect(html).toContain("tabindex=\"0\"");
  });

  it("uses a white surface with border and shadow", () => {
    expect(TOOLTIP_SURFACE_CLASS).toContain("bg-white");
    expect(TOOLTIP_SURFACE_CLASS).toContain("border");
    expect(TOOLTIP_SURFACE_CLASS).toContain("shadow");
    expect(TOOLTIP_SURFACE_CLASS).not.toMatch(/bg-\[#111|bg-black|text-white/);
  });
});

describe("quantity multiplier display", () => {
  it("keeps the label text", () => {
    expect(quantityPolicyLabel({ quantity_mode: "MULTIPLIER", multiplier: 1, fixed_qty: null } as never)).toBe("1x of Master qty");
    expect(quantityPolicyLabel({ quantity_mode: "FIXED", multiplier: 1, fixed_qty: 2 } as never)).toBe("Fixed qty 2");
  });

  function sources(dir: string, out: string[] = []): string[] {
    for (const name of readdirSync(dir)) {
      const full = join(dir, name);
      if (statSync(full).isDirectory()) sources(full, out);
      else if (/\.(tsx|ts)$/.test(name) && !/\.test\./.test(name)) out.push(full);
    }
    return out;
  }

  it("has no dotted underline help styling anywhere, and still opens the quantity dialog", () => {
    const src = join(__dirname, "..");
    for (const file of sources(src)) {
      const text = readFileSync(file, "utf8");
      expect(text, file).not.toMatch(/decoration-dotted|underline-dotted/);
    }
    const page = readFileSync(join(src, "app/(app)/group-strategy/[groupId]/page.tsx"), "utf8");
    expect(page).toContain("setQuantityTarget({ accountId: member.account_id, accountName: displayName, policy })");
    expect(page).toContain("{quantityPolicyLabel(policy)}");
  });

  it("has no black hover tooltip left in the sidebar rail", () => {
    const sidebar = readFileSync(join(__dirname, "../components/layout/Sidebar.tsx"), "utf8");
    const rail = sidebar.slice(sidebar.indexOf("function RailTooltip"), sidebar.indexOf("function ToggleButton"));
    expect(rail).toContain("bg-white");
    expect(rail).not.toMatch(/bg-\[#111111\]|text-white/);
  });
});
