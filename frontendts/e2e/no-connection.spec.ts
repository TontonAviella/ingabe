import { expect, test } from '@playwright/test';

// A phone on a weak connection: every map tile that fails used to add its own red card (each message holds the
// tile's address, so none was "the same error") until the cards covered the map. A dropped connection is said
// once, and the line goes away by itself when the connection is back.
//
// Uses the seeded farm of scripts/e2e_seed.py (see field-flights.spec.ts).
const PROJECT = 'PE2EFLIGHTS1';
const LINE = 'No connection. The map will finish loading when it returns.';

test('a dropped connection is one line, and it leaves when the connection returns', async ({ page, context }, testInfo) => {
  const pageErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  await page.goto(`/project/${PROJECT}`);
  await page.getByRole('button', { name: /1 field outlined · show/ }).waitFor({ timeout: 30_000 });
  await page.locator('.maplibregl-canvas').waitFor();
  await page.waitForTimeout(3000); // the first tiles are in

  await context.setOffline(true);
  // look at ground not loaded yet, several times: many tiles are asked for and none arrives. (The zoom
  // buttons are under the browser's own "You are offline" bar, which must not block them.)
  await page.getByRole('button', { name: 'Zoom in' }).click();
  await page.waitForTimeout(1000);
  await page.getByRole('button', { name: 'Zoom out' }).click();
  await page.getByRole('button', { name: 'Zoom out' }).click();
  await page.waitForTimeout(1000);

  await expect(page.getByText(LINE)).toHaveCount(1, { timeout: 15_000 });
  await expect(page.getByText(/Error loading map data/)).toHaveCount(0);
  await expect(page.getByText(/AJAXError|Failed to fetch/)).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('no-connection.png') });

  await context.setOffline(false);
  await expect(page.getByText(LINE)).toHaveCount(0, { timeout: 15_000 });
  // the map is still drawing: loading the lost tiles again must not break it
  await page.getByRole('button', { name: 'Zoom in' }).click();
  await page.waitForTimeout(3000);
  expect(pageErrors).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath('connection-back.png') });
});
