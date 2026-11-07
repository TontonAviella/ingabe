import { type NextRequest, NextResponse } from "next/server";
import { LOCALE_COOKIE, LOCALE_PATH, type Locale, preferredLocale } from "@/lib/i18n";

// Only the two home pages: sign-in redirects, icons and assets are never touched.
export const config = { matcher: ["/", "/fr"] };

const YEAR = 60 * 60 * 24 * 365;

/**
 * Language at the front door:
 * - "/" in a French-first browser goes to "/fr" (a temporary 307, so search engines keep both pages);
 * - only the EN · FR switch is remembered: it links to "?lang=en" / "?lang=fr", which sets a cookie for a year and
 *   lands on the clean address. Following a /fr link once does not make "/" French for an English browser;
 * - a remembered choice wins over the browser's languages.
 *
 * Both pages are prerendered and Next.js answers them with its own Vary and a long s-maxage, so this redirect needs a
 * host that runs middleware before any shared cache (Vercel does; Cloudflare does not cache HTML by default). Behind a
 * cache that stores HTML, French browsers would get the English page with the EN · FR switch, never a wrong page.
 */
export function middleware(req: NextRequest) {
  const { pathname, searchParams } = req.nextUrl;

  const asked = searchParams.get("lang");
  if (asked === "en" || asked === "fr") {
    const url = req.nextUrl.clone();
    url.searchParams.delete("lang");
    url.pathname = LOCALE_PATH[asked];
    const res = NextResponse.redirect(url);
    res.cookies.set(LOCALE_COOKIE, asked, { maxAge: YEAR, path: "/", sameSite: "lax" });
    return res;
  }

  if (pathname !== "/") return NextResponse.next();

  const chosen = req.cookies.get(LOCALE_COOKIE)?.value;
  const want: Locale = chosen === "en" || chosen === "fr" ? chosen : preferredLocale(req.headers.get("accept-language"));
  if (want === "fr") {
    const url = req.nextUrl.clone();
    url.pathname = LOCALE_PATH.fr;
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}
