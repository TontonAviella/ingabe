// The language of the pages people see before they are signed in (sign-in step, Request access): English or French.
// The website's French pages link with ?lang=fr; otherwise the browser's own first choice between the two decides.

export type PageLang = 'en' | 'fr';

export function pageLang(search = window.location.search, languages: readonly string[] = navigator.languages ?? []): PageLang {
  const asked = new URLSearchParams(search).get('lang');
  if (asked === 'fr' || asked === 'en') return asked;
  for (const tag of languages) {
    const base = tag.toLowerCase().split('-')[0];
    if (base === 'fr' || base === 'en') return base;
  }
  return 'en';
}

/** The same page in the same language: French readers stay in French as they move from page to page. */
export function withLang(path: string, lang: PageLang): string {
  if (lang === 'en') return path;
  return `${path}${path.includes('?') ? '&' : '?'}lang=fr`;
}
