import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

// tsconfig keeps `jsx: "preserve"` and the "@/" path alias for Next.js;
// component tests compile JSX themselves (React automatic runtime) and
// resolve the same alias, without changing the app build.
export default defineConfig({
  oxc: { jsx: { runtime: "automatic" } },
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
});
