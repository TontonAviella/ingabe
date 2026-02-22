import { chromium } from '@playwright/test';

const PROJ_ID = 'test-proj-1';
const MAP_ID  = 'test-map-1';

// Minimal mocked API responses
const MOCK_PROJECT = {
  id: PROJ_ID,
  title: 'Diagnostic Project',
  maps: [MAP_ID],
  created_on: new Date().toISOString(),
};

const MINIMAL_STYLE = JSON.stringify({
  version: 8,
  sources: {},
  layers: [],
  glyphs: 'https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf',
});

const MOCK_MAP_DATA = {
  map_id: MAP_ID,
  project_id: PROJ_ID,
  layers: [],
  changelog: [{ message: 'Initial', map_state: MINIMAL_STYLE, last_edited: new Date().toISOString() }],
};

const MOCK_TREE = {
  project_id: PROJ_ID,
  tree: [{ map_id: MAP_ID, messages: [], fork_reason: null, created_on: new Date().toISOString(), diff_from_previous: null }],
};

console.log('Launching Playwright (headed)...');
const browser = await chromium.launch({ headless: false, args: ['--start-maximized'] });
const ctx     = await browser.newContext({ viewport: null });
const page    = await ctx.newPage();

// Collect all console messages
const consoleLogs = [];
page.on('console', msg => {
  const type = msg.type();
  const text = msg.text();
  const entry = '[' + type.toUpperCase() + '] ' + text;
  consoleLogs.push(entry);
  if (/geoman|terrain|GeoEditor|TerrainControl|draw|Geoman/i.test(text)) {
    console.log('RELEVANT: ' + entry);
  }
  if (type === 'error') {
    console.log('CONSOLE_ERROR: ' + entry);
  }
});

page.on('pageerror', err => {
  console.log('PAGE_ERROR: ' + err.message);
  consoleLogs.push('[PAGE_ERROR] ' + err.message);
});

// Mock all API calls
await page.route('**/api/projects/' + PROJ_ID, route => {
  console.log('Mocking: GET /api/projects/' + PROJ_ID);
  route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_PROJECT) });
});
await page.route('**/api/projects/' + PROJ_ID + '/sources', route => {
  route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
});
await page.route('**/api/conversations**', route => {
  route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
});
await page.route('**/api/maps/' + MAP_ID, route => {
  console.log('Mocking: GET /api/maps/' + MAP_ID);
  route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_MAP_DATA) });
});
await page.route('**/api/maps/' + MAP_ID + '/tree**', route => {
  route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_TREE) });
});
// Block websocket noise
await page.route('**/api/maps/ws/**', route => route.abort());

// Navigate to the project view (OptionalAuth route - no Clerk needed)
const url = 'http://localhost:5173/project/' + PROJ_ID + '/' + MAP_ID;
console.log('Navigating to: ' + url);
await page.goto(url);

// Wait for map canvas to appear
console.log('Waiting for map canvas...');
try {
  await page.waitForSelector('.maplibregl-canvas, canvas', { timeout: 20000 });
  console.log('Map canvas FOUND');
} catch (e) {
  console.log('Map canvas NOT found after 20s: ' + e.message);
}

// Give MapLibre + geoman time to fully initialize
console.log('Waiting 8s for geoman init...');
await page.waitForTimeout(8000);

// Screenshot 1: initial map state
await page.screenshot({ path: '/tmp/mundi-diag-1-map.png', fullPage: false });
console.log('Screenshot 1 saved: /tmp/mundi-diag-1-map.png');

// Inspect DOM state
const domState = await page.evaluate(() => {
  return {
    mapCanvasCount: document.querySelectorAll('.maplibregl-canvas').length,
    pageTitle: document.title,
    hasGeomanCSS: Array.from(document.styleSheets).some(ss => {
      try { return ss.href && ss.href.includes('geoman'); } catch (e) { return false; }
    }),
    geomanElementsFound: Array.from(document.querySelectorAll('[class*="gm-"], .gm-toolbar')).map(el => el.className.substring(0, 60)),
    controlGridPresent: !!document.querySelector('[class*="control-grid"]'),
    maplibreControlButtons: Array.from(document.querySelectorAll('.maplibregl-ctrl button')).map(b => ({
      text: b.textContent.trim().substring(0, 30),
      title: b.title,
      className: b.className.substring(0, 50),
    })),
    allCanvases: Array.from(document.querySelectorAll('canvas')).map(c => ({ class: c.className, width: c.width, height: c.height })),
  };
});

console.log('DOM state: ' + JSON.stringify(domState, null, 2));

// Try to expand control grid if present
const ctrlBtns = await page.locator('.maplibregl-ctrl button').all();
console.log('Found ' + ctrlBtns.length + ' maplibre control buttons');

if (ctrlBtns.length > 0) {
  // List all buttons
  for (let i = 0; i < Math.min(ctrlBtns.length, 8); i++) {
    const t = await ctrlBtns[i].textContent().catch(() => '');
    const ti = await ctrlBtns[i].getAttribute('title').catch(() => '');
    const cl = await ctrlBtns[i].getAttribute('class').catch(() => '');
    console.log('Button ' + i + ': text="' + t.trim() + '" title="' + ti + '" class="' + cl + '"');
  }

  // Click first button to try opening control grid
  try {
    await ctrlBtns[0].click({ timeout: 3000 });
    await page.waitForTimeout(2000);
    await page.screenshot({ path: '/tmp/mundi-diag-2-after-click.png', fullPage: false });
    console.log('Screenshot 2 (after button click): /tmp/mundi-diag-2-after-click.png');
  } catch (e) {
    console.log('Could not click control button: ' + e.message);
  }
}

// Look for draw-related elements after interaction
const drawState = await page.evaluate(() => {
  const candidates = [
    '[class*="draw"]', '[class*="geo-editor"]', '[class*="gm-"]',
    '[title*="draw"]', '[title*="Draw"]', '[title*="polygon"]',
    '[class*="geoman"]', '[class*="toolbar"]',
  ];
  const results = [];
  for (const sel of candidates) {
    const els = document.querySelectorAll(sel);
    if (els.length > 0) {
      results.push({ selector: sel, count: els.length, firstClass: els[0].className.substring(0, 80), firstTitle: els[0].getAttribute('title') });
    }
  }
  return results;
});
console.log('Draw-related elements: ' + JSON.stringify(drawState, null, 2));

// Check terrain elements
const terrainState = await page.evaluate(() => {
  const els = document.querySelectorAll('[class*="terrain"], [title*="terrain"], [title*="Terrain"], [title*="3D"]');
  return Array.from(els).map(el => ({
    tag: el.tagName,
    class: el.className.substring(0, 80),
    title: el.getAttribute('title'),
    text: el.textContent.trim().substring(0, 30),
  }));
});
console.log('Terrain elements: ' + JSON.stringify(terrainState, null, 2));

// Report all captured console logs
console.log('=== ALL CONSOLE MESSAGES ===');
consoleLogs.forEach(l => console.log(l));

// Final screenshot
await page.screenshot({ path: '/tmp/mundi-diag-3-final.png', fullPage: false });
console.log('Screenshot 3 (final): /tmp/mundi-diag-3-final.png');

// Keep browser open 20s for visual inspection
console.log('Keeping browser open 20s...');
await page.waitForTimeout(20000);

await browser.close();
console.log('DONE.');
