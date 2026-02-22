import posthog from 'posthog-js';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import './index.css';
import '@geoman-io/maplibre-geoman-free/dist/maplibre-geoman.css'; // Geoman draw primitives
import { init } from '@mundi/ee';
import App from './App';

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
