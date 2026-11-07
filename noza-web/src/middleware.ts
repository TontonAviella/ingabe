import { type NextRequest, NextResponse } from "next/server";
import { LOCALE_COOKIE, type Locale, preferredLocale } from "@/lib/i18n";

// Only the two home pages: sign-in redirects, icons and assets are never touched.
export const config = { matcher: ["/", "/fr"] };

const YEAR = 60 * 60 * 24 * 365;

function remember(res: NextResponse, locale: Locale) {
  res.cookies.set(LOCALE_COOKIE, locale, { maxAge: YEAR, path: "/", sameSite: "lax" });
  return res;
}

/**
 * Language at the front door:
 * - "/" in a French-first browser goes to "/fr" (a temporary 307, so search engines keep both pages);
 * - a reader's own choice (the EN · FR switch) wins over the browser and is kept in a cookie for a year;
 * - "/?lang=en" is how the switch records "English, please" before landing on "/".
 */
export function middleware(req: NextRequest) {
  const { pathname, searchParams } = req.nextUrl;
  const chosen = req.cookies.get(LOCALE_COOKIE)?.value;

  if (pathname === "/fr") {
    const res = NextResponse.next();
    return chosen === "fr" ? res : remember(res, "fr");
  }

  if (searchParams.get("lang") === "en") {
    const url = req.nextUrl.clone();
    url.searchParams.delete("lang");
    return remember(NextResponse.redirect(url), "en");
  }

  const want: Locale = chosen === "en" || chosen === "fr" ? chosen : preferredLocale(req.headers.get("accept-language"));
  if (want === "fr") {
    const url = req.nextUrl.clone();
    url.pathname = "/fr";
    return NextResponse.redirect(url);
  }
  const res = NextResponse.next();
  res.headers.set("Vary", "Accept-Language, Cookie");
  return res;
}
