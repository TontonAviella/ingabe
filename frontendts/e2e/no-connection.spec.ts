import { expect, type Page, test } from '@playwright/test';

// A phone on a weak connection: every map tile that fails used to add its own red card (each message holds the
// tile's address, so none was "the same error") until the cards covered the map. A dropped connection is said
// once, and the line goes away by itself when the connection is back.
//
// Uses the seeded farm of scripts/e2e_seed.py (see field-flights.spec.ts).
const PROJECT = 'PE2EFLIGHTS1';
const LINE = 'No connection. The map will finish loading when it returns.';

// The drawer's row of tools (Layers, Questions, Fields, Sage): there once the map's page is up.
const tools = (page: Page) => page.getByRole('navigation', { name: 'Map tools' });

test('a dropped connection is one line, and it leaves when the connection returns', async ({ page, context }, testInfo) => {
  test.setTimeout(90_000); // its waits alone add up to more than the usual 30 s on a busy machine
  const pageErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  await page.goto(`/project/${PROJECT}`);
  await tools(page).waitFor({ timeout: 30_000 });
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

// The same weak connection, for everything that is not the map: a request that never arrives was shown in the
// browser's own words ("Failed to fetch") in the field panel, the cards and Sage, and a gateway's in its own
// ("Bad Gateway", "Request failed (502)").
const RAW = /Failed to fetch|AJAXError|TypeError|NetworkError|Load failed|Request failed|Bad Gateway|Error loading map data/;

test('requests that never arrive are said in plain words, in Sage too', async ({ page }, testInfo) => {
  await page.goto(`/project/${PROJECT}`);
  await tools(page).waitFor({ timeout: 30_000 });
  await page.waitForTimeout(3000);

  // the browser still believes it is online, as a phone with one bar does: nothing gets through
  await page.route('**/*', (route) => route.abort('internetdisconnected'));
  await tools(page).getByRole('button', { name: 'Sage' }).click();
  const ask = page.getByRole('textbox', { name: 'Ask Sage', exact: true }); // Sage's own box, not the cards'
  await ask.fill('How healthy is this field?');
  await ask.press('Enter');

  await expect(page.getByText('No connection. Try again when it returns.').first()).toBeVisible({ timeout: 20_000 });
  // Sage is not left saying it is working on a question that was never sent
  await expect(page.getByText('Creating new conversation...')).toHaveCount(0, { timeout: 15_000 });
  await expect(page.getByText(RAW)).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('sage-no-connection.png') });
});

test('a gateway error is a plain line with its number', async ({ page }, testInfo) => {
  await page.goto(`/project/${PROJECT}`);
  await tools(page).waitFor({ timeout: 30_000 });
  await page.waitForTimeout(3000);

  await page.route('**/api/**', (route) =>
    route.fulfill({ status: 502, contentType: 'text/html', body: '<html><h1>502 Bad Gateway</h1></html>' }),
  );
  await tools(page).getByRole('button', { name: 'Sage' }).click();
  const ask = page.getByRole('textbox', { name: 'Ask Sage', exact: true }); // Sage's own box, not the cards'
  await ask.fill('How healthy is this field?');
  await ask.press('Enter');

  await expect(page.getByText('The server is not answering right now (502). Try again in a moment.').first()).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.getByText(RAW)).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('sage-bad-gateway.png') });
});

test('on a phone the line is in the open, not under another panel', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 780 });
  await page.goto(`/project/${PROJECT}`);
  await page.locator('.maplibregl-canvas').waitFor({ timeout: 30_000 });
  await page.waitForTimeout(3000);

  await page.route('**/*', (route) => route.abort('internetdisconnected'));
  await page.getByRole('button', { name: 'Zoom out' }).click();
  await page.getByRole('button', { name: 'Zoom out' }).click();

  const line = page.getByText(LINE);
  await expect(line).toHaveCount(1, { timeout: 15_000 });
  // nothing lies over it: the element at each end of the line is the line itself (or inside it)
  const uncovered = await line.evaluate((el) => {
    const box = el.getBoundingClientRect();
    return [0.1, 0.5, 0.9].every((part) => {
      const top = document.elementFromPoint(box.left + box.width * part, box.top + box.height / 2);
      return !!top && (el === top || el.contains(top) || top.contains(el));
    });
  });
  expect(uncovered).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('phone-no-connection.png') });
});
