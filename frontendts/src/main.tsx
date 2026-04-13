import posthog from 'posthog-js';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import './index.css';
import '@geoman-io/maplibre-geoman-free/dist/maplibre-geoman.css'; // Geoman draw primitives
import { init } from '@mundi/ee';
import App from './App';

// After a deploy, the old JS chunk filenames no longer exist on the server.
// If a user has the tab open during a deploy, lazy imports will fail with
// "Failed to fetch dynamically imported module". Auto-reload once to pick up
// the new assets. The sessionStorage guard prevents an infinite reload loop.
window.addEventListener('vite:preloadError', (e) => {
  e.preventDefault();
  if (!sessionStorage.getItem('chunk-reload')) {
    sessionStorage.setItem('chunk-reload', '1');
    window.location.reload();
  }
});
// Clear the guard on successful page load so future deploys can retry
sessionStorage.removeItem('chunk-reload');

// Initialize PostHog analytics (only when key is provided)
const posthogKey = import.meta.env.VITE_POSTHOG_KEY;
if (posthogKey) {
  posthog.init(posthogKey, {
    api_host: import.meta.env.VITE_POSTHOG_HOST || 'https://us.i.posthog.com',
    autocapture: true,
    capture_pageview: true,
    capture_pageleave: true,
    persistence: 'localStorage+cookie',
  });
}

init()
  .then(() => {
    createRoot(document.getElementById('root')!).render(
      <StrictMode>
        <App />
      </StrictMode>,
    );
  })
  .catch((e: unknown) => {
    // eslint-disable-next-line no-console
    console.error('[EE] init failed', e);
    const rootEl = document.getElementById('root')!;
    createRoot(rootEl).render(
      <StrictMode>
        <div style={{ padding: 24 }}>
          <h1>Initialization error</h1>
          <p>Authentication/EE initialization failed. Please refresh the page. If the issue persists, contact support.</p>
        </div>
      </StrictMode>,
    );
  });
