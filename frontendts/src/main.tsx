import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import './index.css';
import '@geoman-io/maplibre-geoman-free/dist/maplibre-geoman.css'; // Geoman draw primitives
import { init } from '@mundi/ee';
import App from './App';
import { initAnalytics, trackError } from './lib/analytics';

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

try {
  initAnalytics();
} catch (e: unknown) {
  console.error('[Analytics] init failed', e);
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
    trackError('app_init_failed', e);
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
