import { expect, test } from '@playwright/test';

// Hiding a layer did nothing on a slow connection (the live site, 2026-11-19): the hide was only applied when
// the map had nothing left to load, and with tiles still on their way it was dropped. The layer's name was
// struck through and the layer stayed on the map.
//
// Uses the seeded farm of scripts/e2e_seed.py (see field-flights.spec.ts): two flights, both drawn.
const PROJECT = 'PE2EFLIGHTS1';
const APRIL = 'LE2EAPRIL001';
const MARCH = 'LE2EMARCH001';

test('a layer hidden while the map is still loading is hidden', async ({ page }) => {
  test.setTimeout(90_000);
  const aprilTiles: string[] = [];
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname;
    if (path.includes(`/api/layer/${APRIL}/`) && path.endsWith('.png')) aprilTiles.push(path);
  });
  // the other flight's tiles never arrive, so the map always has something left to load
  await page.route(new RegExp(`/api/layer/${MARCH}/\\d+/\\d+/\\d+\\.png`), () => {});

  await page.goto(`/project/${PROJECT}`);
  await page.locator('.maplibregl-canvas').waitFor({ timeout: 30_000 });
  await expect.poll(() => aprilTiles.length, { timeout: 30_000 }).toBeGreaterThan(0); // it is drawn to begin with

  const row = page
    .locator('div.group')
    .filter({ has: page.locator('input[value="April flight"], :text-is("April flight")') })
    .first();
  await row.hover();
  await row.getByRole('button', { name: 'Hide layer' }).click();
  await expect(row.getByRole('button', { name: 'Show layer' })).toBeVisible();
  await page.waitForTimeout(1000);

  // look at ground not loaded yet: a hidden layer asks for no tiles
  const before = aprilTiles.length;
  await page.getByRole('button', { name: 'Zoom out' }).click();
  await page.waitForTimeout(1500);
  await page.getByRole('button', { name: 'Zoom out' }).click();
  await page.waitForTimeout(3000);
  expect(aprilTiles.length).toBe(before);

  // and it comes back when shown again
  await row.hover();
  await row.getByRole('button', { name: 'Show layer' }).click();
  await expect.poll(() => aprilTiles.length, { timeout: 15_000 }).toBeGreaterThan(before);
});
