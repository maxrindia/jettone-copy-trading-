import { afterEach, describe, expect, it, vi } from "vitest";
import { copyToClipboard } from "./clipboard";

const URL_TO_COPY = "https://app.example.test/platform/dematconnected";

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubDocument(execResult: boolean) {
  const area = { value: "", style: {} as Record<string, string>, setAttribute: vi.fn(), select: vi.fn() };
  const doc = {
    createElement: vi.fn(() => area),
    body: { appendChild: vi.fn(), removeChild: vi.fn() },
    execCommand: vi.fn(() => execResult),
  };
  vi.stubGlobal("document", doc);
  return { area, doc };
}

describe("copyToClipboard", () => {
  it("writes the exact text through the async clipboard API", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    expect(await copyToClipboard(URL_TO_COPY)).toBe(true);
    expect(writeText).toHaveBeenCalledWith(URL_TO_COPY);
  });

  it("falls back to execCommand with the exact text when the clipboard API is missing (plain HTTP)", async () => {
    vi.stubGlobal("navigator", {});
    const { area, doc } = stubDocument(true);
    expect(await copyToClipboard(URL_TO_COPY)).toBe(true);
    expect(area.value).toBe(URL_TO_COPY);
    expect(doc.execCommand).toHaveBeenCalledWith("copy");
    expect(doc.body.removeChild).toHaveBeenCalledWith(area);
  });

  it("reports failure instead of pretending it copied", async () => {
    vi.stubGlobal("navigator", { clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) } });
    stubDocument(false);
    expect(await copyToClipboard(URL_TO_COPY)).toBe(false);
    expect(await copyToClipboard("")).toBe(false);
  });
});
