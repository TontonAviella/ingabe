// Errors in people's browsers, sent to the server (/api/client-error) so they show on the Ingabe Health page and
// in Grafana: script errors, promise rejections nobody caught, a screen that crashed (the error boundary), and the
// app failing to start. Each different message is sent once per page and at most 10 per page, so a loop cannot
// flood the server. Noise the browser makes on its own (extensions, ResizeObserver, aborted requests, a deploy's
// old chunks, which main.tsx already handles by reloading) is not sent.

export type BrowserErrorKind = 'js_error' | 'promise_error' | 'react_crash' | 'app_init_failed';

const MAX_PER_PAGE = 10;
const sent = new Set<string>();

const IGNORED = [
  /ResizeObserver loop/i,
  /AbortError|The user aborted a request|signal is aborted|operation was aborted|Fetch is aborted/i,
  /Failed to fetch dynamically imported module|Importing a module script failed/i,
  /^Script error\.?$/i, // another site's script: the browser hides the details
];

function build(): string {
  const main = document.querySelector<HTMLScriptElement>('script[type="module"][src*="/assets/"]');
  return main?.src.split('/').pop()?.slice(0, 80) ?? '';
}

function topFrames(stack: string | undefined): string {
  if (!stack) return '';
  // Keep file names and positions, not full URLs: shorter, and the same for every user.
  return stack
    .split('\n')
    .slice(0, 6)
    .map((line) => line.replace(/https?:\/\/[^/\s)]+\/(?:assets\/)?/g, ''))
    .join('\n')
    .slice(0, 1500);
}

export function reportBrowserError(kind: BrowserErrorKind, error: unknown, source = ''): void {
  try {
    const message = (error instanceof Error ? error.message : typeof error === 'string' ? error : String(error ?? '')).slice(0, 500);
    // A cancelled request (a timeout, leaving the page) is not a fault; browsers word it differently, the name is the same.
    if ((error as { name?: string } | null)?.name === 'AbortError') return;
    if (IGNORED.some((re) => re.test(message))) return;
    if (/^(chrome|moz|safari)-extension:/.test(source)) return;
    const key = `${kind}|${message}`;
    if (sent.has(key) || sent.size >= MAX_PER_PAGE) return;
    sent.add(key);
    const body = {
      kind,
      message,
      page: window.location.pathname.slice(0, 300),
      source: source.replace(/https?:\/\/[^/\s]+\/(?:assets\/)?/, '').slice(0, 300),
      stack: topFrames(error instanceof Error ? error.stack : undefined),
      build: build(),
    };
    void fetch('/api/client-error', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      keepalive: true,
    }).catch(() => undefined);
  } catch {
    // Reporting an error must never cause another one.
  }
}

/** Listen for errors nothing else caught; call once, before the app renders. */
export function watchBrowserErrors(): void {
  window.addEventListener('error', (event: ErrorEvent) => {
    if (!event.message && !event.error) return; // a failed image or script load, not a script error
    const where = event.filename ? `${event.filename}:${event.lineno}:${event.colno}` : '';
    reportBrowserError('js_error', event.error ?? event.message, where);
  });
  window.addEventListener('unhandledrejection', (event: PromiseRejectionEvent) => {
    reportBrowserError('promise_error', event.reason);
  });
}
