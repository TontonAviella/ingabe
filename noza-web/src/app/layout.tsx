import type { Metadata, Viewport } from "next";
import { Instrument_Serif, JetBrains_Mono } from "next/font/google";
import "./globals.css";

const display = Instrument_Serif({
  subsets: ["latin"],
  weight: "400",
  style: ["normal", "italic"],
  variable: "--font-display",
});

const mono = JetBrains_Mono({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-mono",
});

export const metadata: Metadata = {
  title: "Noza Labs · Reading the land and the lines that cross it",
  description:
    "Drone and satellite intelligence for Rwanda's farms, towers and power lines. Every answer says how sure it is.",
  keywords: ["drone", "agriculture", "Rwanda", "power line inspection", "tower inspection", "crop map", "Ingabe"],
};

export const viewport: Viewport = {
  themeColor: "#F6F1EB",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={`${display.variable} ${mono.variable}`}>
      <body className="font-sans">{children}</body>
    </html>
  );
}
