import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { SIDEBAR_COLLAPSED_PX, SIDEBAR_EXPANDED_PX, SIDEBAR_EXTRA_PX } from "./sidebar-metrics";

describe("sidebar metrics", () => {
  it("match the widths Sidebar.tsx actually renders", () => {
    const src = readFileSync(join(__dirname, "Sidebar.tsx"), "utf8");
    expect(src).toContain(`w-[${SIDEBAR_EXPANDED_PX}px]`);
    expect(src).toContain(`w-[${SIDEBAR_COLLAPSED_PX}px]`);
  });

  it("publish the width a collapsed sidebar frees up", () => {
    expect(SIDEBAR_EXTRA_PX).toBe(SIDEBAR_EXPANDED_PX - SIDEBAR_COLLAPSED_PX);
    expect(SIDEBAR_EXTRA_PX).toBeGreaterThan(0);
  });
});
