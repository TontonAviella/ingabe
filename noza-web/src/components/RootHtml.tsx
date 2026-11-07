import type { Metadata } from "next";
import { Instrument_Serif, JetBrains_Mono } from "next/font/google";
import { DICTS, LOCALE_PATH, type Locale } from "@/lib/i18n";
import { SITE_URL } from "@/lib/site";

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

/** Title, description and the language twins (hreflang), so search engines show French readers the French page. */
export function siteMetadata(locale: Locale): Metadata {
  const m = DICTS[locale].meta;
  return {
    metadataBase: new URL(SITE_URL),
    title: m.title,
    description: m.description,
    keywords: m.keywords,
    alternates: {
      canonical: LOCALE_PATH[locale],
      languages: { en: LOCALE_PATH.en, fr: LOCALE_PATH.fr, "x-default": LOCALE_PATH.en },
    },
    openGraph: { title: m.title, description: m.description, locale: locale === "fr" ? "fr_FR" : "en_GB", siteName: "Noza Labs" },
  };
}

/** The <html> shell for one language: each language group ("(en)", "(fr)") is its own root layout, so lang is right. */
export function RootHtml({ locale, children }: { locale: Locale; children: React.ReactNode }) {
  return (
    <html lang={locale} className={`${display.variable} ${mono.variable}`}>
      <body className="font-sans">{children}</body>
    </html>
  );
}
