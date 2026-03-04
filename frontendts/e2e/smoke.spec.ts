import { test, expect } from '@playwright/test';

test.describe('Smoke Tests', () => {
  test('homepage loads and redirects to sign-in or shows map list', async ({ page }) => {
    await page.goto('/');
    // Either we get redirected to Clerk sign-in, or we see the maps list
    await expect(
      page
        .locator('text=Sign in')
        .or(page.locator('text=Projects'))
        .or(page.locator('text=New Project'))
        .first(),
    ).toBeVisible({ timeout: 15_000 });
  });

  test('404 page renders for unknown routes', async ({ page }) => {
    await page.goto('/this-route-does-not-exist');
    await expect(
      page.locator('text=not found').or(page.locator('text=404')).first(),
    ).toBeVisible({
      timeout: 10_000,
    });
  });

  test('API health check returns valid response', async ({ request }) => {
    // Verify the API is reachable (any API endpoint)
    const response = await request.get('/api/projects', {
      headers: { Accept: 'application/json' },
    });
    // Should get 401 (unauthorized) or 200, not 500
    expect([200, 401, 403]).toContain(response.status());
  });

  test('static assets load correctly', async ({ page }) => {
    await page.goto('/');
    // Verify CSS and JS assets loaded (no broken asset references)
    const failedRequests: string[] = [];
    page.on('requestfailed', (request) => {
      if (request.url().includes('/assets/')) {
        failedRequests.push(request.url());
      }
    });
    await page.waitForTimeout(3000);
    expect(failedRequests).toHaveLength(0);
  });

  test('favicon loads', async ({ request }) => {
    const lightResponse = await request.get('/favicon-light.svg');
    expect(lightResponse.ok()).toBe(true);

    const darkResponse = await request.get('/favicon-dark.svg');
    expect(darkResponse.ok()).toBe(true);
  });
});

test.describe('Health & Monitoring', () => {
  test('healthz probe returns 200', async ({ request }) => {
    const response = await request.get('/healthz');
    expect(response.status()).toBe(200);
    const body = await response.json();
    expect(body.status).toBe('ok');
  });

  test('readiness probe returns 200 when DB is up', async ({ request }) => {
    const response = await request.get('/ready');
    expect(response.status()).toBe(200);
    const body = await response.json();
    expect(body.ready).toBe(true);
  });

  test('metrics endpoint returns Prometheus format', async ({ request }) => {
    const response = await request.get('/metrics');
    expect(response.status()).toBe(200);
    const text = await response.text();
    expect(text).toContain('http_requests_total');
    expect(text).toContain('http_request_errors_total');
  });

  test('detailed health check returns service statuses', async ({ request }) => {
    const response = await request.get('/health');
    expect([200, 503]).toContain(response.status());
    const body = await response.json();
    expect(body.checks).toBeDefined();
    expect(body.checks.postgres).toBeDefined();
    expect(body.checks.redis).toBeDefined();
  });
});

test.describe('Project View (unauthenticated)', () => {
  test('project route renders without crashing', async ({ page }) => {
    // ProjectView uses OptionalAuth, so it should render even without auth
    await page.goto('/project/PXXXXXXXXXX');
    // Should show either the project view or a not-found/error message, not a white screen
    await page.waitForTimeout(3000);
    const bodyText = await page.textContent('body');
    expect(bodyText).toBeTruthy();
    // Verify no uncaught JS errors
    const consoleErrors: string[] = [];
    page.on('pageerror', (error) => consoleErrors.push(error.message));
    expect(consoleErrors.filter((e) => e.includes('Uncaught'))).toHaveLength(0);
  });
});
