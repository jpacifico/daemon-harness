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

test('the window scripts never declare the same top-level function twice', () => {
  // These scripts share the window's global scope: a second declaration
  // silently replaces the first, and the page that relied on it breaks.
  const scripts = [...read('index.html').matchAll(/<script\b[^>]*\bsrc="([^"]+\.js)"/g)].map(match => match[1]);
  assert.ok(scripts.includes('renderer.js'));
  const seen = new Map();
  for (const file of scripts) {
    for (const match of read(file).matchAll(/^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(/gm)) {
      const name = match[1];
      assert.ok(!seen.has(name), `${name} is declared in ${seen.get(name)} and again in ${file}`);
      seen.set(name, file);
    }
  }
  assert.ok(seen.size > 50);
});
