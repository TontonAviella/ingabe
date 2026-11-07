"use client";

import { createContext, useContext } from "react";
import { DICTS, type Dict, type Locale } from "@/lib/i18n";

const LocaleContext = createContext<{ locale: Locale; t: Dict }>({ locale: "en", t: DICTS.en });

/** The page's language for every client component below it (the server page picks it from the URL). */
export function LocaleProvider({ locale, children }: { locale: Locale; children: React.ReactNode }) {
  return <LocaleContext.Provider value={{ locale, t: DICTS[locale] }}>{children}</LocaleContext.Provider>;
}

export const useLocale = () => useContext(LocaleContext);
