import { expect, test } from '@playwright/test';

// The one drawer beside the map (src/components/MapDrawer.tsx), on unless a device asked for the old placing with ?drawer=0: the tools live in one
// column, one at a time, and nothing lies on the photo. Same seeded farm as field-flights.spec.ts.
const PROJECT = 'PE2EFLIGHTS1';

test.describe('One drawer for the map tools', () => {
  test('a field is read in the drawer while the map stays clear of it', async ({ page }, testInfo) => {
    const pageErrors: string[] = [];
    page.on('pageerror', (error) => pageErrors.push(error.message));

    await page.goto(`/project/${PROJECT}?drawer=1`);
    const drawer = page.getByTestId('map-drawer');
    const tools = drawer.getByRole('navigation', { name: 'Map tools' });
    await expect(tools).toBeVisible({ timeout: 30_000 });

    // the old floating controls are gone: the drawer does their work
    await expect(page.getByRole('button', { name: /Hide panels|Show panels/ })).toHaveCount(0);

    await tools.getByRole('button', { name: 'Fields' }).click();
    await drawer.getByRole('button', { name: /1 field outlined · show/ }).click({ timeout: 30_000 });
    await drawer.getByRole('button', { name: /Field 1 · 0\.36 ha/ }).click();
    const flights = drawer.getByText('This field on each flight').locator('xpath=following-sibling::ul[1]').getByRole('listitem');
    await expect(flights.nth(0)).toHaveText('14 March 2026: health index (NDVI) 0.50', { timeout: 30_000 });
    await expect(flights.nth(1)).toHaveText('2 April 2026 (this photo): health index (NDVI) 0.80', { timeout: 30_000 });

    // beside the map, not on it: the drawer ends where the map begins
    const map = page.locator('.map-container');
    const drawerBox = await drawer.boundingBox();
    const mapOpen = await map.boundingBox();
    expect(drawerBox && mapOpen && drawerBox.x + drawerBox.width <= mapOpen.x + 1).toBe(true);

    // one tool at a time: another tool takes the drawer, and the field's panel is put away, not lost
    await tools.getByRole('button', { name: 'Layers' }).click();
    await expect(drawer.getByText('This field on each flight')).toBeHidden();
    await expect(drawer.locator('[data-tool="layers"]').getByRole('button', { name: 'Share' })).toBeVisible();
    await tools.getByRole('button', { name: 'Fields' }).click();
    await expect(flights.nth(0)).toBeVisible();

    const openPicture = testInfo.outputPath('drawer-open.png');
    await page.screenshot({ path: openPicture });
    await testInfo.attach('drawer-open', { path: openPicture, contentType: 'image/png' });

    // closing the drawer gives the map the room: this is what "Hide panels" was for
    await drawer.getByRole('button', { name: 'Close the drawer' }).click();
    await expect(drawer.getByText('This field on each flight')).toBeHidden();
    await expect.poll(async () => (await map.boundingBox())?.width ?? 0).toBeGreaterThan((mapOpen?.width ?? 0) + 300);

    const closedPicture = testInfo.outputPath('drawer-closed.png');
    await page.screenshot({ path: closedPicture });
    await testInfo.attach('drawer-closed', { path: closedPicture, contentType: 'image/png' });
    expect(pageErrors).toHaveLength(0);
  });
  test('while a field is outlined, what is drawn on the map for reading steps aside', async ({ page }) => {
    await page.goto(`/project/${PROJECT}?drawer=1`);
    const drawer = page.getByTestId('map-drawer');
    await drawer.getByRole('navigation', { name: 'Map tools' }).getByRole('button', { name: 'Fields' }).click({ timeout: 30_000 });
    const onMap = page.getByTestId('map-panels');
    await expect(onMap).toHaveClass(/contents/);
    await drawer.getByRole('button', { name: 'Outline a field' }).click({ timeout: 30_000 });
    // a tap must reach the photo: legends and close-look bubbles are put away, as they were before the drawer
    await expect(onMap).toHaveClass(/hidden/);
    await expect(drawer.getByText('Tap inside the field.')).toBeVisible();
    await drawer.getByRole('button', { name: 'Close', exact: true }).click();
    await expect(onMap).toHaveClass(/contents/);
  });

  // Roger, 2026-11-23: people could not work out how to use Sage. Its side reads like the chats people know: what
  // can be asked is on the screen, and the question box is the last thing in the column.
  test("Sage's side offers questions to start from, with the question box under them", async ({ page }, testInfo) => {
    await page.goto(`/project/${PROJECT}?drawer=1`);
    const drawer = page.getByTestId('map-drawer');
    await drawer.getByRole('navigation', { name: 'Map tools' }).getByRole('button', { name: 'Sage' }).click({ timeout: 30_000 });
    const sage = drawer.locator('[data-tool="sage"]');
    await expect(sage.getByRole('heading', { name: 'What do you want to know?' })).toBeVisible();
    const first = sage.getByRole('button', { name: 'How healthy are the crops on this photo?' });
    await expect(first).toBeVisible();
    await expect(sage.getByRole('button', { name: 'Has it rained enough around this farm this season?' })).toBeVisible();

    // one box, at the foot of the column, with a button that sends: nothing to send yet, so it waits
    const box = sage.getByRole('textbox', { name: 'Ask Sage' });
    const send = sage.getByRole('button', { name: 'Send to Sage' });
    await expect(send).toBeDisabled();
    const [questionAt, boxAt, drawerAt] = [await first.boundingBox(), await box.boundingBox(), await drawer.boundingBox()];
    expect(questionAt && boxAt && boxAt.y > questionAt.y + questionAt.height).toBe(true);
    expect(boxAt && drawerAt && drawerAt.y + drawerAt.height - (boxAt.y + boxAt.height) < 40).toBe(true);
    await box.fill('How big is this photo?');
    await expect(send).toBeEnabled();

    // nothing here repeats what the drawer already does
    await expect(sage.getByRole('button', { name: 'Hide chats' })).toHaveCount(0);
    await expect(sage.getByRole('region', { name: "Sage's answer" })).toHaveCount(0);

    const picture = testInfo.outputPath('sage-start.png');
    await page.screenshot({ path: picture });
    await testInfo.attach('sage-start', { path: picture, contentType: 'image/png' });
  });

  test('a closed drawer stays closed after a reload', async ({ page }) => {
    await page.goto(`/project/${PROJECT}?drawer=1`);
    const drawer = page.getByTestId('map-drawer');
    const tools = drawer.getByRole('navigation', { name: 'Map tools' });
    await tools.getByRole('button', { name: 'Layers' }).click({ timeout: 30_000 });
    // whichever way it started, end with the drawer closed
    if (await drawer.getByRole('button', { name: 'Close the drawer' }).isVisible()) {
      await drawer.getByRole('button', { name: 'Close the drawer' }).click();
    }
    await expect(drawer.getByRole('button', { name: 'Close the drawer' })).toBeHidden();
    await page.reload();
    await expect(tools).toBeVisible({ timeout: 30_000 });
    await expect(drawer.getByRole('button', { name: 'Close the drawer' })).toBeHidden();
    // and the switch is kept without the address saying so again
    expect(new URL(page.url()).searchParams.get('drawer')).toBe('1');
  });
});

test.describe('One drawer on a narrow screen', () => {
  test.use({ viewport: { width: 390, height: 780 } });

  test('the tools are a bar along the bottom and the field is read in a sheet above it', async ({ page }, testInfo) => {
    await page.goto(`/project/${PROJECT}?drawer=1`);
    const drawer = page.getByTestId('map-drawer');
    const tools = drawer.getByRole('navigation', { name: 'Map tools' });
    await expect(tools).toBeVisible({ timeout: 30_000 });
    const bar = await tools.boundingBox();
    expect(bar && bar.y + bar.height >= 770 && bar.width >= 380).toBe(true);

    await tools.getByRole('button', { name: 'Fields' }).click();
    await drawer.getByRole('button', { name: /1 field outlined · show/ }).click({ timeout: 30_000 });
    await drawer.getByRole('button', { name: /Field 1 · 0\.36 ha/ }).click();
    await expect(drawer.getByText('This field on each flight')).toBeVisible();
    // the sheet takes at most half the screen (and the bar): the upper half of the map stays free
    const sheet = await drawer.boundingBox();
    expect(sheet && sheet.y >= 780 * 0.4).toBe(true);

    const picture = testInfo.outputPath('drawer-phone.png');
    await page.screenshot({ path: picture });
    await testInfo.attach('drawer-phone', { path: picture, contentType: 'image/png' });
  });
});
