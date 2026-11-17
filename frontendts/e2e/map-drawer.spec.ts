import { expect, test } from '@playwright/test';

// The one drawer beside the map (src/components/MapDrawer.tsx), asked for with ?drawer=1: the tools live in one
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
});
