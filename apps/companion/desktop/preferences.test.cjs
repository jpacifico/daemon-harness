"use strict";
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const {Preferences} = require('./preferences.cjs');
async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'companion-theme-'));
  t.after(() => fs.rm(root, {recursive:true, force:true}));
  return new Preferences(root);
}
test('r6 language settings migrate without losing French; theme survives concurrent language changes', async t => {
  const prefs = await fixture(t);
  await fs.writeFile(prefs.file, JSON.stringify({schema:1,language:'fr'}));
  await prefs.load();
  assert.equal(prefs.language,'fr'); assert.equal(prefs.theme,'light');
  await Promise.all([prefs.setTheme('dark'),prefs.set('en'),prefs.set('fr')]);
  const reopened = new Preferences(path.dirname(prefs.file)); await reopened.load();
  assert.equal(reopened.language,'fr'); assert.equal(reopened.theme,'dark');
  await Promise.all([reopened.set('en'),reopened.setTheme('system')]);
  assert.deepEqual(JSON.parse(await fs.readFile(prefs.file,'utf8')),{schema:1,language:'en',theme:'system',background:false,openAtLogin:false});
  assert.deepEqual(await fs.readdir(path.dirname(prefs.file)),['display-preferences.json']);
});
test('invalid themes and failed writes leave the last saved preferences intact and allow retry', async t => {
  const prefs = await fixture(t); await prefs.setTheme('dark');
  const saved = await fs.readFile(prefs.file,'utf8');
  for(const invalid of ['../../light','auto',null,{},0])await assert.rejects(prefs.setTheme(invalid));
  assert.equal(await fs.readFile(prefs.file,'utf8'),saved);
  await fs.unlink(prefs.file); await fs.mkdir(prefs.file);
  await assert.rejects(prefs.setTheme('system'));
  assert.equal(prefs.theme,'dark');
  assert.deepEqual(await fs.readdir(path.dirname(prefs.file)),['display-preferences.json']);
  await fs.rmdir(prefs.file); await prefs.setTheme('light');
  assert.equal(prefs.theme,'light');
});
test('unrecognized stored themes preserve a valid language and default to light', async t => {
  const prefs = await fixture(t);
  await fs.writeFile(prefs.file,JSON.stringify({schema:1,language:'fr',theme:'unexpected'}));
  await prefs.load(); assert.equal(prefs.language,'fr'); assert.equal(prefs.theme,'light');
});
test('background mode is off by default, saved with login, and login needs background', async () => {
  const dir = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'daemons-prefs-bg-')));
  const prefs = new Preferences(dir);
  await prefs.load();
  assert.equal(prefs.background, false); assert.equal(prefs.openAtLogin, false);
  await prefs.setBackground(false, true);
  assert.equal(prefs.openAtLogin, false);
  await prefs.setBackground(true, true);
  const again = new Preferences(dir); await again.load();
  assert.equal(again.background, true); assert.equal(again.openAtLogin, true);
  await assert.rejects(prefs.setBackground('yes', true));
  await fs.rm(dir, {recursive: true, force: true});
});
