import { chromium } from 'playwright-core';

const browser = await chromium.launch({
  channel: 'chrome',
  headless: true,
  args: ['--headless=new', '--use-gl=angle', '--use-angle=metal'],
});
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

const allMsgs = [];
page.on('console', msg => {
  const text = msg.text();
  allMsgs.push(`[${msg.type()}] ${text}`);
});

const pageErrors = [];
page.on('pageerror', err => {
  pageErrors.push(err.message);
  console.log(`  PAGE ERROR: ${err.message.substring(0, 500)}`);
});

// Intercept script evaluation errors
page.on('requestfailed', req => {
  console.log(`  FAILED: ${req.url().substring(0, 200)} - ${req.failure()?.errorText}`);
});

console.log('Test 1: Loading Docker app...');
const response = await page.goto('http://localhost:8000/project/PNYtRfxgUQw5', {
  waitUntil: 'load',
  timeout: 60000
});
console.log(`Response status: ${response?.status()}`);
console.log(`Response URL: ${response?.url()}`);

await page.waitForTimeout(5000);

// Check if JS bundle loaded and executed
const jsState = await page.evaluate(() => {
  return {
    rootChildren: document.getElementById('root')?.children.length ?? -1,
    scriptCount: document.querySelectorAll('script').length,
    // Check if React/ReactDOM are available
    hasReact: typeof window.__REACT_DEVTOOLS_GLOBAL_HOOK__ !== 'undefined',
    // Check for SuperTokens or auth state
    cookies: document.cookie,
    localStorage_keys: Object.keys(localStorage),
    sessionStorage_keys: Object.keys(sessionStorage),
    // Check window errors
    windowError: window.__lastError,
  };
});
console.log('\nJS State:', JSON.stringify(jsState, null, 2));

console.log('\nAll console messages:');
allMsgs.forEach(m => console.log(`  ${m.substring(0, 300)}`));

console.log('\nPage errors:');
pageErrors.forEach(m => console.log(`  ${m.substring(0, 500)}`));

await page.screenshot({ path: '/Users/macbook/Ingabe/mundi.ai/frontendts/test-controls-screenshot.png' });
console.log('\nScreenshot saved');
await browser.close();
