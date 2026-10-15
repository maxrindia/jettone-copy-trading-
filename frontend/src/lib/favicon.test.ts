import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// Next.js App Router serves src/app/icon.png as the site icon (/icon.png) and
// links it in <head> automatically; layout.tsx must not override it.
describe("favicon asset", () => {
  const iconPath = join(__dirname, "../app/icon.png");
  const png = readFileSync(iconPath);

  it("is a valid square PNG", () => {
    expect(png.subarray(0, 8).toString("hex")).toBe("89504e470d0a1a0a");
    const width = png.readUInt32BE(16);
    const height = png.readUInt32BE(20);
    expect(width).toBe(height);
    expect(width).toBeGreaterThanOrEqual(192);
  });

  it("stays small enough for a favicon", () => {
    expect(png.length).toBeLessThan(200 * 1024);
  });

  it("is not overridden by layout metadata, and the title stays JETTONE", () => {
    const layout = readFileSync(join(__dirname, "../app/layout.tsx"), "utf8");
    expect(layout).not.toMatch(/icons\s*:/);
    expect(layout).toContain('title: "JETTONE"');
  });
});
