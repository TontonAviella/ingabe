#!/usr/bin/env node
const geoViewport = require('@mapbox/geo-viewport');

let inputData = '';

process.stdin.on('data', (chunk) => {
  inputData += chunk;
});

process.stdin.on('end', () => {
  try {
    const payload = JSON.parse(inputData);

    if (!payload.bbox || !payload.width || !payload.height) {
      throw new Error('Missing required parameters: bbox, width, and height are required');
    }

    const boundsArr = typeof payload.bbox === 'string'
      ? payload.bbox.split(',').map(Number)
      : payload.bbox;

    const viewport = geoViewport.viewport(
      boundsArr,
      [payload.width, payload.height],
      undefined,
      undefined,
      512,
      true
    );

    console.log(JSON.stringify({
      zoom: viewport.zoom,
      center: viewport.center
    }));
  } catch (error) {
    console.error('Error:', error.message);
    process.exit(1);
  }
});

process.stdin.resume();

