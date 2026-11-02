import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type React from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { vi } from 'vitest';

// Small helpers for component tests: React 18's own root, queries over the whole document (dialogs render in a
// portal), and waits that poll until React Query and React have settled. @testing-library/dom (the peer of
// @testing-library/react) is not installed, so these stand in for the few queries the tests need.

// Updates arrive from resolved fetches outside act(); polling waits for them, so React need not warn about act.
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = false;

let mounted: { root: Root; host: HTMLElement }[] = [];

/** Renders `ui` with a fresh query client (no retries) and returns its host element. */
export function mount(ui: React.ReactElement): HTMLElement {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const root = createRoot(host);
  root.render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
  mounted.push({ root, host });
  return host;
}

export function unmountAll() {
  for (const { root, host } of mounted) {
    root.unmount();
    host.remove();
  }
  mounted = [];
  document.body.innerHTML = '';
}

function normalise(text: string | null | undefined): string {
  return (text ?? '').replace(/\s+/g, ' ').trim();
}

function matches(text: string, want: string | RegExp): boolean {
  return typeof want === 'string' ? text === want : want.test(text);
}

/** The deepest elements whose whole text matches. */
export function allByText(want: string | RegExp, selector = '*'): HTMLElement[] {
  const hits = [...document.body.querySelectorAll<HTMLElement>(selector)].filter((el) => matches(normalise(el.textContent), want));
  return hits.filter((el) => !hits.some((other) => other !== el && el.contains(other)));
}

export function byText(want: string | RegExp, selector = '*'): HTMLElement | null {
  return allByText(want, selector)[0] ?? null;
}

/** A button by its label (aria-label, else its text). */
export function byButton(name: string | RegExp): HTMLButtonElement | null {
  const buttons = [...document.body.querySelectorAll<HTMLButtonElement>('button')];
  return buttons.find((b) => matches(normalise(b.getAttribute('aria-label') ?? b.textContent), name)) ?? null;
}

export function byRole(role: string, name?: string | RegExp): HTMLElement | null {
  const els = [...document.body.querySelectorAll<HTMLElement>(`[role="${role}"]`)];
  return els.find((el) => name === undefined || matches(normalise(el.getAttribute('aria-label') ?? el.textContent), name)) ?? null;
}

const CONTROLS = 'button, a[href], input:not([type="hidden"]), textarea, select, [role="button"], [role="link"]';

/**
 * Every button, link and field on the page that a screen reader would announce with no name: no text, no
 * aria-label, no aria-labelledby, no title (an icon-only button that lost its aria-label lands here). Controls
 * hidden from screen readers (aria-hidden, a bot trap) are skipped. A test asserts this is empty.
 */
export function unnamedControls(): string[] {
  return [...document.body.querySelectorAll<HTMLElement>(CONTROLS)]
    .filter((el) => !el.closest('[aria-hidden="true"]'))
    .filter((el) => {
      const labelledBy = (el.getAttribute('aria-labelledby') ?? '')
        .split(/\s+/)
        .map((id) => document.getElementById(id)?.textContent ?? '')
        .join(' ');
      const pressable = el instanceof HTMLInputElement && ['submit', 'button', 'reset'].includes(el.type);
      const field = !pressable && (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement);
      // A field is named by its <label>s, never by what was typed in it; an <input type="submit"> by its value;
      // anything else by its text, including the name of a picture inside it (alt, or a named role="img" like the logo).
      const text = field
        ? [...(el.labels ?? [])].map((l) => l.textContent).join(' ')
        : pressable
          ? (el as HTMLInputElement).value
          : [
              el.textContent,
              ...[...el.querySelectorAll('img[alt]')].map((img) => img.getAttribute('alt')),
              ...[...el.querySelectorAll('[role="img"][aria-label]')].map((img) => img.getAttribute('aria-label')),
            ].join(' ');
      return !normalise([el.getAttribute('aria-label'), labelledBy, text, el.getAttribute('title')].join(' '));
    })
    .map((el) => el.outerHTML.slice(0, 120));
}

/** Waits until `find` returns an element. */
export function findBy<T>(find: () => T | null, what: string): Promise<T> {
  return vi.waitFor(
    () => {
      const found = find();
      if (!found) throw new Error(`Not found: ${what}`);
      return found;
    },
    { timeout: 3000, interval: 20 },
  );
}

export const findText = (want: string | RegExp, selector?: string) => findBy(() => byText(want, selector), String(want));
export const findButton = (name: string | RegExp) => findBy(() => byButton(name), `button ${String(name)}`);
export const findRole = (role: string, name?: string | RegExp) => findBy(() => byRole(role, name), `[role=${role}] ${String(name ?? '')}`);

export function click(el: Element) {
  el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 }));
}

export function pointer(el: Element, type: 'pointerdown' | 'pointermove' | 'pointerup', clientX: number, clientY: number) {
  el.dispatchEvent(
    new PointerEvent(type, { bubbles: true, cancelable: true, pointerId: 1, pointerType: 'mouse', button: 0, clientX, clientY }),
  );
}
