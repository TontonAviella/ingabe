import { describe, expect, it } from 'vitest';
import { pageLang, withLang } from '@/lib/pageLang';

describe('pageLang', () => {
  it("follows ?lang= first, then the browser's first choice between English and French", () => {
    expect(pageLang('?lang=fr', ['en-US'])).toBe('fr');
    expect(pageLang('?lang=en', ['fr-SN'])).toBe('en');
    expect(pageLang('', ['fr-SN', 'fr', 'en'])).toBe('fr');
    expect(pageLang('', ['wo-SN', 'fr-SN'])).toBe('fr');
    expect(pageLang('', ['de-DE'])).toBe('en');
    expect(pageLang('?lang=xx', [])).toBe('en');
  });

  it('keeps French readers in French from page to page', () => {
    expect(withLang('/request-access', 'fr')).toBe('/request-access?lang=fr');
    expect(withLang('/auth/login?confirmed=1', 'fr')).toBe('/auth/login?confirmed=1&lang=fr');
    expect(withLang('/request-access', 'en')).toBe('/request-access');
  });
});
