'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const {Backups, KEEP_AUTOMATIC, RETRY_AFTER_FAILURE} = require('./backup-service.cjs');
const {decrypt} = require('./backup-crypto.cjs');

const password = 'synthetic-long-password';
// Electron safeStorage stand-in: real encryption, no plaintext on disk.
function safeStorage() {
  const key = crypto.randomBytes(32);
  return {isEncryptionAvailable: () => true,
    encryptString: value => { const iv = crypto.randomBytes(12), c = crypto.createCipheriv('aes-256-gcm', key, iv); return Buffer.concat([iv, c.update(value, 'utf8'), c.final(), c.getAuthTag()]); },
    decryptString: bytes => { const c = crypto.createDecipheriv('aes-256-gcm', key, bytes.subarray(0, 12)); c.setAuthTag(bytes.subarray(-16)); return Buffer.concat([c.update(bytes.subarray(12, -16)), c.final()]).toString('utf8'); }};
}
class FakeDrive {
  constructor() { this.files = []; this.uploads = []; this.removed = []; }
  async upload(file, {label}) { const bytes = await fs.readFile(file); this.uploads.push({bytes, label});
    this.files.unshift({id: 'f' + this.files.length + 'x', kind: label.kind, daemon: label.daemon, name: label.name, createdAt: new Date(Date.now() + this.files.length).toISOString(), bytes: bytes.length});
    this.files.sort((a, b) => b.createdAt.localeCompare(a.createdAt)); return {bytes: bytes.length}; }
  async backups() { return [...this.files]; }
  async remove(id, daemon) { const f = this.files.find(x => x.id === id); assert.equal(f.kind, 'automatic'); assert.equal(f.daemon, daemon); this.files = this.files.filter(x => x.id !== id); this.removed.push(id); }
}
async function fixture(t, {fail = null} = {}) {
  const root = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'daemon-backups-test-')));
  t.after(() => fs.rm(root, {recursive: true, force: true}));
  const row = {daemon: {id: 'a'.repeat(20), memory: '/synthetic/vault', name: 'Nova'}, healthPaths: {spool: '/synthetic/spool'}};
  const requests = [];
  const backend = {async daemonSettings(request, mode) {
    requests.push({request, mode});
    if (fail) throw new Error(fail);
    await fs.writeFile(request.destination, 'PK synthetic archive');
    return {files: 3, bytes: 20, attempts: 1, failed_turns: 0};
  }};
  const instances = {async daemonContext(id) { assert.equal(id, 'instance-1'); return {row}; }};
  let now = Date.parse('2026-10-27T08:00:00Z');
  const tokens = {};
  class OAuth { constructor({store}) { this.store = store; } async signIn() { await this.store.put({refresh_token: 'synthetic'}); return {connected: true}; }
    async token() { return 'synthetic-access'; } async disconnect() { await this.store.delete(); } }
  const backups = new Backups({userData: root, instances, backend, safeStorage: safeStorage(), platform: 'darwin',
    oauthConfig: {client_id: 'x'}, GoogleOAuth: OAuth, Drive: FakeDrive, now: () => now});
  return {backups, root, requests, drive: backups.drive, tick: ms => { now += ms; }, tokens};
}

test('without a Google client, Drive stays unavailable and file backups still work', async t => {
  const {root} = await fixture(t);
  const plain = new Backups({userData: root, instances: {daemonContext: async () => ({row: {daemon: {id: 'b'.repeat(20)}}})}, backend: {}, safeStorage: safeStorage(), platform: 'darwin'});
  assert.deepEqual(await plain.status(), {configured: false, connected: false});
  await assert.rejects(plain.toDrive('instance-1', {password}), /google.notConfigured/);
});

test('a manual Drive backup is encrypted with the password and labelled manual', async t => {
  const {backups, drive, requests, root} = await fixture(t);
  await backups.connect();
  const result = await backups.toDrive('instance-1', {password});
  assert.equal(result.drive, true);
  assert.deepEqual(drive.uploads[0].label, {kind: 'manual', daemon: 'a'.repeat(20), name: 'Nova'});
  assert.equal(drive.uploads[0].bytes.includes(Buffer.from('synthetic archive')), false);
  assert.deepEqual(requests[0], {request: {action: 'backup', vault: '/synthetic/vault', spool: '/synthetic/spool', destination: requests[0].request.destination}, mode: 'backup'});
  // No plain or encrypted copy is left behind.
  assert.deepEqual((await fs.readdir(path.join(root, 'backups'))).filter(n => !['secrets'].includes(n)), []);
});

test('daily backups run once a day, open with the password, rotate and never touch manual ones', async t => {
  const {backups, drive, tick, root} = await fixture(t);
  await backups.connect();
  await backups.toDrive('instance-1', {password});
  await backups.enableAutomatic('instance-1', password);
  assert.equal((await backups.status('instance-1')).automatic.enabled, true);
  for (let day = 0; day < KEEP_AUTOMATIC + 3; day++) { await backups.runDue(); await backups.runDue(); tick(25 * 3600 * 1000); }
  const automatic = drive.files.filter(f => f.kind === 'automatic');
  assert.equal(drive.uploads.length, 1 + KEEP_AUTOMATIC + 3);
  assert.equal(automatic.length, KEEP_AUTOMATIC);
  assert.equal(drive.files.filter(f => f.kind === 'manual').length, 1);
  const sample = path.join(root, 'sample');
  await fs.writeFile(sample + '.enc', drive.uploads.at(-1).bytes);
  await decrypt(sample + '.enc', sample + '.zip', password);
  assert.equal(await fs.readFile(sample + '.zip', 'utf8'), 'PK synthetic archive');
  // The key is in protected storage only, never in plain JSON.
  const files = await fs.readdir(path.join(root, 'backups/secrets'));
  for (const name of files) assert.equal((await fs.readFile(path.join(root, 'backups/secrets', name))).includes(Buffer.from('salt')), false);
  assert.equal((await fs.readFile(path.join(root, 'backups/automatic.json'), 'utf8')).includes(password), false);
});

test('a failure is recorded for the daemon page and retried; a busy daemon just waits', async t => {
  const {backups} = await fixture(t, {fail: 'drive.permissionOrQuota'});
  await backups.connect();
  await backups.enableAutomatic('instance-1', password);
  await backups.runDue();
  assert.equal((await backups.status('instance-1')).automatic.lastError, 'drive.permissionOrQuota');
  const waiting = await fixture(t, {fail: 'backup.daemon-busy'});
  await waiting.backups.connect();
  await waiting.backups.enableAutomatic('instance-1', password);
  await waiting.backups.runDue();
  assert.equal((await waiting.backups.status('instance-1')).automatic.lastError, null);
});

test('turning daily backup on needs Google; disconnecting removes keys and stops it', async t => {
  const {backups, root} = await fixture(t);
  await assert.rejects(backups.enableAutomatic('instance-1', password), /google.signInAgain/);
  await backups.connect();
  await assert.rejects(backups.enableAutomatic('instance-1', 'short'), /passwordLength/);
  await backups.enableAutomatic('instance-1', password);
  await backups.disconnect();
  assert.deepEqual(await backups.status('instance-1'), {configured: true, connected: false, automatic: {enabled: false, lastSuccess: null, lastError: null}});
  assert.deepEqual(await fs.readdir(path.join(root, 'backups/secrets')), []);
});

test('a failed automatic backup waits six hours before trying again', async t => {
  const {backups, requests, tick} = await fixture(t, {fail: 'drive.permissionOrQuota'});
  await backups.connect();
  await backups.enableAutomatic('instance-1', password);
  await backups.runDue();
  assert.equal(requests.length, 1);
  for (let hour = 1; hour < RETRY_AFTER_FAILURE / 3600000; hour++) { tick(3600000); await backups.runDue(); }
  assert.equal(requests.length, 1);
  tick(3600000);
  await backups.runDue();
  assert.equal(requests.length, 2);
});
