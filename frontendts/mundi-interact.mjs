/**
 * Playwright interaction test for geoman drawing + terrain.
 * Mocks the backend, renders a real map, clicks the draw + terrain buttons,
 * and reports whether they work.
 */
import { chromium } from '@playwright/test';
import { readFileSync } from 'fs';
import { resolve } from 'path';

const PROJ_ID = 'test-proj-1';
const MAP_ID  = 'test-map-1';

const MINIMAL_STYLE = JSON.stringify({
  version: 8,
  sources: {},
  layers: [],
  glyphs: 'https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf',
});

const MOCK_PROJECT = { id: PROJ_ID, title: 'Diagnostic Project', maps: [MAP_ID], created_on: new Date().toISOString() };
const MOCK_MAP_DATA = { map_id: MAP_ID, project_id: PROJ_ID, layers: [], changelog: [{ message: 'Initial', map_state: MINIMAL_STYLE, last_edited: new Date().toISOString() }] };
const MOCK_TREE = { project_id: PROJ_ID, tree: [{ map_id: MAP_ID, messages: [], fork_reason: null, created_on: new Date().toISOString(), diff_from_previous: null }] };

const DEPS_DIR = '/Users/macbook/Ingabe/mundi.ai/frontendts/node_modules/.vite/deps';

const browser = await chromium.launch({ headless: false, args: ['--start-maximized'] });
const ctx = await browser.newContext({ viewport: null });
const page = await ctx.newPage();

// Force-serve modified dep chunks from disk (bypasses Vite in-memory cache)
await page.route('**/chunk-Y2U6EPKB.js**', route => {
  const content = readFileSync(resolve(DEPS_DIR, 'chunk-Y2U6EPKB.js'), 'utf8');
  route.fulfill({ status: 200, contentType: 'application/javascript; charset=utf-8', body: content });
});

const logs = [];
page.on('console', msg => {
  const entry = '[' + msg.type().toUpperCase() + '] ' + msg.text();
  logs.push(entry);
  // Print GeoEditor, terrain, draw, and errors
  if (/GeoEditor|geoman|terrain|draw|error/i.test(entry) || msg.type() === 'error' || msg.type() === 'warn') {
    console.log('>> ' + entry);
  }
});
page.on('pageerror', err => {
  const entry = '[PAGE_ERROR] ' + err.message;
  logs.push(entry);
  console.log('>> ' + entry);
});

// Mock API
await page.route('**/api/projects/' + PROJ_ID, r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_PROJECT) }));
await page.route('**/api/projects/' + PROJ_ID + '/sources', r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) }));
await page.route('**/api/conversations**', r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) }));
await page.route('**/api/maps/' + MAP_ID, r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_MAP_DATA) }));
await page.route('**/api/maps/' + MAP_ID + '/tree**', r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_TREE) }));
await page.route('**/api/maps/ws/**', r => r.abort());

await page.goto('http://localhost:5173/project/' + PROJ_ID + '/' + MAP_ID);
console.log('Navigated. Waiting for canvas...');
await page.waitForSelector('.maplibregl-canvas', { timeout: 20000 });
console.log('Canvas found. Waiting 12s for full init (geoman + deck.gl)...');
await page.waitForTimeout(12000);

// ── GeoEditor init diagnostics ────────────────────────────────────────────────
const geomanLogs = logs.filter(l => /GeoEditor|geoman/i.test(l));
console.log('\n=== GeoEditor logs ===');
geomanLogs.forEach(l => console.log(l));
if (geomanLogs.length === 0) {
  console.log('(no GeoEditor logs found — _autoInitGeoman may not have been called)');
}

// ── Inspect DOM state ─────────────────────────────────────────────────────────
const domState = await page.evaluate(() => {
  const polygonBtn = document.querySelector('[title="Polygon"]');
  const expandBtn = document.querySelector('[title="Expand toolbar"], .geo-editor-collapse-btn');
  const gmElements = document.querySelectorAll('[id^="gm_"], [class*="gm_"], [class*="geoman"]');
  const geoEditorControl = document.querySelector('.geo-editor-control');

  return {
    polygonBtnExists: !!polygonBtn,
    polygonBtnVisible: polygonBtn ? polygonBtn.offsetParent !== null : false,
    polygonBtnClass: polygonBtn ? polygonBtn.className : null,
    expandBtnExists: !!expandBtn,
    expandBtnTitle: expandBtn ? expandBtn.title : null,
    expandBtnVisible: expandBtn ? expandBtn.offsetParent !== null : false,
    gmElementCount: gmElements.length,
    gmElementSummary: Array.from(gmElements).slice(0, 5).map(el => ({ id: el.id, cls: el.className.substring(0, 50) })),
    geoEditorExists: !!geoEditorControl,
  };
});
console.log('\n=== DOM State ===');
console.log(JSON.stringify(domState, null, 2));

await page.screenshot({ path: '/tmp/mundi-interact-1-before.png' });
console.log('Screenshot 1: /tmp/mundi-interact-1-before.png');

// ── TERRAIN TEST ──────────────────────────────────────────────────────────────
console.log('\n--- Testing TERRAIN ---');
const terrainBtn = page.locator('[title="Enable terrain"]').first();
if (await terrainBtn.count() > 0) {
  await terrainBtn.click();
  await page.waitForTimeout(3000);
  const terrainState = await page.evaluate(() => {
    const btn = document.querySelector('[title="Enable terrain"]') || document.querySelector('[title="Disable terrain"]');
    return { title: btn ? btn.title : null, className: btn ? btn.className : null };
  });
  console.log('Terrain state after click:', JSON.stringify(terrainState));
  await page.screenshot({ path: '/tmp/mundi-interact-2-terrain.png' });
  console.log('Screenshot 2: /tmp/mundi-interact-2-terrain.png');
} else {
  console.log('No terrain button found!');
}

// ── EXPAND TOOLBAR THEN DRAW ──────────────────────────────────────────────────
console.log('\n--- Expanding GeoEditor toolbar ---');

// The GeoEditor toolbar starts collapsed; we must click "Expand toolbar" first
const expandBtn = page.locator('[title="Expand toolbar"], .geo-editor-collapse-btn').first();
const expandCount = await expandBtn.count();
console.log('Expand toolbar button found:', expandCount);

if (expandCount > 0) {
  const isVisible = await expandBtn.isVisible();
  console.log('Expand button visible:', isVisible);
  if (isVisible) {
    await expandBtn.click();
    console.log('Clicked expand toolbar');
    await page.waitForTimeout(1000);
    await page.screenshot({ path: '/tmp/mundi-interact-3-expanded.png' });
    console.log('Screenshot 3: /tmp/mundi-interact-3-expanded.png');
  }
}

// ── DRAW TEST ────────────────────────────────────────────────────────────────
console.log('\n--- Testing DRAW (Polygon) ---');
// Use .last() to target the visible expanded toolbar (not the collapsed one)
const polygonBtn = page.locator('[title="Polygon"][data-mode="polygon"]').last();
const polygonCount = await polygonBtn.count();
console.log('Polygon draw buttons found:', polygonCount);

if (polygonCount > 0) {
  const isVisible = await polygonBtn.isVisible();
  console.log('Polygon button visible:', isVisible);

  if (isVisible) {
    const drawLogs = [];
    const drawListener = msg => drawLogs.push('[' + msg.type().toUpperCase() + '] ' + msg.text());
    page.on('console', drawListener);

    await polygonBtn.click({ timeout: 5000 });
    console.log('Clicked polygon button');
    await page.waitForTimeout(2000);

    page.off('console', drawListener);
    console.log('Draw-related logs after click:');
    drawLogs.filter(l => /draw|polygon|geoman|mode|enable|active|error/i.test(l)).forEach(l => console.log('  ' + l));

    const drawState = await page.evaluate(() => {
      const polygonBtn = document.querySelector('[title="Polygon"][data-mode="polygon"]');
      const activeBtn = document.querySelector('.geo-editor-tool-button--act');
      const gmLayers = document.querySelectorAll('[id^="gm_"]');
      const cursor = getComputedStyle(document.querySelector('.maplibregl-canvas') || document.body).cursor;
      return {
        polygonBtnActive: polygonBtn ? polygonBtn.classList.contains('geo-editor-tool-button--act') : null,
        polygonBtnClass: polygonBtn ? polygonBtn.className : null,
        activeToolTitle: activeBtn ? activeBtn.getAttribute('title') : null,
        gmLayerCount: gmLayers.length,
        mapCursorStyle: cursor,
      };
    });
    console.log('Draw state after Polygon click:');
    console.log(JSON.stringify(drawState, null, 2));

    await page.screenshot({ path: '/tmp/mundi-interact-4-drawing.png' });
    console.log('Screenshot 4: /tmp/mundi-interact-4-drawing.png');

    // Try drawing on the canvas
    console.log('\nAttempting to draw polygon on map...');
    const canvas = page.locator('.maplibregl-canvas').first();
    const box = await canvas.boundingBox();
    if (box) {
      const cx = box.x + box.width / 2;
      const cy = box.y + box.height / 2;
      await page.mouse.click(cx, cy);
      await page.waitForTimeout(500);
      await page.mouse.click(cx + 80, cy + 60);
      await page.waitForTimeout(500);
      await page.mouse.click(cx - 60, cy + 80);
      await page.waitForTimeout(500);
      await page.mouse.dblclick(cx - 60, cy + 80); // finish polygon
      await page.waitForTimeout(1000);
      await page.screenshot({ path: '/tmp/mundi-interact-5-drawn.png' });
      console.log('Screenshot 5: /tmp/mundi-interact-5-drawn.png');

      const drawResult = await page.evaluate(() => {
        const gmElements = document.querySelectorAll('[id^="gm_"]');
        return {
          gmElementCount: gmElements.length,
          gmElementIds: Array.from(gmElements).slice(0, 10).map(el => el.id),
        };
      });
      console.log('GM elements after drawing:', JSON.stringify(drawResult, null, 2));
    }
  } else {
    console.log('Polygon button still not visible after expanding toolbar!');
    // Debug: dump all geo-editor buttons
    const allBtns = await page.evaluate(() => {
      return Array.from(document.querySelectorAll('.geo-editor-tool-button')).map(el => ({
        title: el.title,
        visible: el.offsetParent !== null,
        classes: el.className,
      }));
    });
    console.log('All geo-editor buttons:');
    allBtns.forEach(b => console.log(' ', JSON.stringify(b)));
  }
} else {
  console.log('No polygon button found!');
}

// ── FULL LOG SUMMARY ──────────────────────────────────────────────────────────
console.log('\n=== FULL CONSOLE LOG (errors + GeoEditor) ===');
logs.filter(l => l.includes('[ERROR]') || l.includes('[WARN]') || /GeoEditor|geoman/i.test(l)).forEach(l => console.log(l));

await page.screenshot({ path: '/tmp/mundi-interact-6-final.png' });
console.log('\nFinal screenshot: /tmp/mundi-interact-6-final.png');

console.log('\nKeeping browser open 15s...');
await page.waitForTimeout(15000);
await browser.close();
console.log('DONE.');
