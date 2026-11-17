import { expect, test } from '@playwright/test';

// One real flow, as a person does it: open a farm, pick a field, read what the field panel says of it on each
// flight. The farm is the one scripts/e2e_seed.py makes: two made-up pictures of the same ground whose right
// answer is known beforehand (health index 0.50 in March, 0.80 in April), and one kept field.
//
// The app under test has no sign-in and its own database (scripts/e2e_app.sh locally, the E2E steps in
// .github/workflows/cicd.yml in CI). Without the seeded farm these tests fail: run the seed first.
const PROJECT = 'PE2EFLIGHTS1';

test.describe('A field across the flights of its place', () => {
  test('the panel sets the two flights side by side, each with its own number', async ({ page }, testInfo) => {
    const pageErrors: string[] = [];
    page.on('pageerror', (error) => pageErrors.push(error.message));

    await page.goto(`/project/${PROJECT}`);

    // the kept field is found from the panel's own list, as a reader finds it
    await page.getByRole('button', { name: /1 field outlined · show/ }).click({ timeout: 30_000 });
    await page.getByRole('button', { name: /Field 1 · 0\.36 ha/ }).click();

    const heading = page.getByText('This field on each flight');
    await expect(heading).toBeVisible();
    const flights = heading.locator('xpath=following-sibling::ul[1]').getByRole('listitem');
    await expect(flights).toHaveCount(2);

    // oldest first; the other flight is measured beside the request, so its line is waited for
    await expect(flights.nth(0)).toHaveText('14 March 2026: health index (NDVI) 0.50', { timeout: 30_000 });
    await expect(flights.nth(1)).toHaveText('2 April 2026 (this photo): health index (NDVI) 0.80', { timeout: 30_000 });

    // side by side, with no verdict: the note says why, and no line says better or worse
    await expect(page.getByText(/the flights are given side by side\s+with no verdict/)).toBeVisible();
    await expect(flights.filter({ hasText: /better|worse|improved|declined/i })).toHaveCount(0);

    // the day this photo was flown is the reader's own, shown as they gave it
    await expect(page.getByLabel('Day this photo was flown')).toHaveValue('2026-04-02');

    // the evidence: what the reader saw, kept in test-results/ whether the test passes or not
    const picture = testInfo.outputPath('field-panel.png');
    await page.screenshot({ path: picture });
    await testInfo.attach('field-panel', { path: picture, contentType: 'image/png' });
    expect(pageErrors).toHaveLength(0);
  });
});
