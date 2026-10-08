import type { Config } from "tailwindcss";

// The Ingabe palette (Roger, 2026-10-06): black, chocolate and caramel; no green in the interface,
// crops are the only green. Night side for dark bands, cream side for daylight bands.
const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        night: "#0B0908",
        espresso: "#17110E",
        chocolate: "#221813",
        cocoa: "#4A3326",
        caramel: "#D9A066",
        sand: "#F3EDE6",
        latte: "#B8A99B",
        cream: "#F6F1EB",
        bitter: "#1A1310",
        mocha: "#6B5A4E",
        bark: "#4A2E1F",
      },
      fontFamily: {
        display: ["var(--font-display)", "Georgia", "serif"],
        sans: [
          "-apple-system",
          "BlinkMacSystemFont",
          '"SF Pro Text"',
          '"Helvetica Neue"',
          "Inter",
          "Arial",
          "sans-serif",
        ],
        mono: ["var(--font-mono)", "SFMono-Regular", "Menlo", "monospace"],
      },
      letterSpacing: {
        label: "0.22em",
      },
      maxWidth: {
        page: "86rem",
      },
    },
  },
  plugins: [],
};
export default config;
