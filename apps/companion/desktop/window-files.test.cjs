'use strict';
// The window refuses any file outside main.cjs's allow-list, so a script or
// stylesheet added to index.html but missing from it never loads.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const read = name => fs.readFileSync(path.join(__dirname, name), 'utf8');

test('every file index.html loads is allowed by the window', () => {
  const list = /const allowed = new Set\(\[([^\]]*)\]/.exec(read('main.cjs'));
  assert.ok(list, 'allow-list not found in main.cjs');
  const allowed = new Set([...list[1].matchAll(/'([^']+)'/g)].map(match => match[1]));
  const html = read('index.html');
  const loaded = [...html.matchAll(/<(?:script|link)\b[^>]*\b(?:src|href)="([^"]+)"/g)].map(match => match[1]);
  assert.ok(loaded.length >= 4);
  for (const file of loaded) {
    assert.ok(allowed.has(file), `${file} is loaded by index.html but not allowed in main.cjs`);
    assert.ok(fs.existsSync(path.join(__dirname, file)), `${file} does not exist`);
  }
  const packaged = JSON.parse(read('package.json')).build.files;
  for (const file of allowed) assert.ok(packaged.includes(file), `${file} is not packaged`);
});
