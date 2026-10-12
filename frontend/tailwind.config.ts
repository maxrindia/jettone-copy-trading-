import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        // Jettone brand: black + white + orange. Orange is the primary CTA /
        // key trading-control colour; green and red stay semantic only.
        jettone: {
          orange: "#E88900",
          "orange-hover": "#D97700",
          ink: "#111111",
          surface: "#F7F7F7",
        },
        themeDark: "#0F0F10",
        themeBlack: "#000000",
        themeWhite: "#FFFFFF",
        themeGray: {
          50: "#F9FAFB",
          100: "#F3F4F6",
          200: "#E5E7EB",
          300: "#D1D5DB",
          400: "#9CA3AF",
          500: "#6B7280",
          600: "#4B5563",
          700: "#374151",
          800: "#1F2937",
          900: "#0F0F10",
        },
      },
      // Page width caps that grow with a collapsed sidebar (see AppMain).
      maxWidth: {
        page: "calc(120rem + var(--sidebar-extra, 0px))",
        "page-wide": "calc(120rem + var(--sidebar-extra, 0px))",
      },
      fontFamily: {
        sans: ["Inter Variable", "Inter", "sans-serif"],
      },
    },
  },
  plugins: [],
};
export default config;
