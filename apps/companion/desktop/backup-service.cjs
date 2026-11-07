'use strict';
// Memory backups: to a file or to Google Drive, by hand or once a day.
// The archive exists in plain form only in a private folder until encrypted.
// Passwords are never stored; an automatic backup keeps only a derived key in
// the system's protected store. Companion owns this, not the daemon.
const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const backupCrypto = require('./backup-crypto.cjs');
const {ProtectedStore} = require('./backup-secrets.cjs');

const DAY = 24 * 60 * 60 * 1000;
// A failed automatic backup is retried at most every six hours, not at every hourly check.
const RETRY_AFTER_FAILURE = 6 * 60 * 60 * 1000;
const KEEP_AUTOMATIC = 7;
const EXTENSION = 'daemonbackup';

function checkedPassword(password) {
  if (typeof password !== 'string' || [...password].length < 12 || Buffer.byteLength(password) > 1024) throw new Error('backup.passwordLength');
  return password;
}
function fileName(name) { return String(name || '').replace(/[^\p{L}\p{N} _-]/gu, '').trim().slice(0, 40) || 'daemon'; }

class Backups {
  constructor({userData, instances, backend, safeStorage, platform = process.platform, oauthConfig = null, open = null,
    fetchImpl = fetch, now = () => Date.now(), GoogleOAuth = require('./backup-google.cjs').GoogleOAuth, Drive = require('./backup-drive.cjs').Drive}) {
    this.root = path.join(userData, 'backups');
    this.instances = instances; this.backend = backend; this.safe = safeStorage; this.platform = platform; this.now = now;
    this.tokens = new ProtectedStore(path.join(this.root, 'secrets'), safeStorage, platform);
    this.oauth = null;
    if (oauthConfig) {
      try { this.oauth = new GoogleOAuth({config: oauthConfig, store: this.tokens, open, fetchImpl}); } catch { this.oauth = null; }
    }
    this.drive = this.oauth ? new Drive({token: options => this.oauth.token(options), fetchImpl}) : null;
    this.running = null;
  }
  async folder(prefix) {
    await fs.mkdir(this.root, {recursive: true, mode: 0o700});
    return fs.realpath(await fs.mkdtemp(path.join(await fs.realpath(this.root), prefix)));
  }
  keyStore(daemon) { return new ProtectedStore(path.join(this.root, 'secrets'), this.safe, this.platform, `key-${daemon}.bin`); }
  async schedule() {
    try { const value = JSON.parse(await fs.readFile(path.join(this.root, 'automatic.json'), 'utf8')); return value?.schema === 1 && value.daemons && typeof value.daemons === 'object' ? value : {schema: 1, daemons: {}}; }
    catch { return {schema: 1, daemons: {}}; }
  }
  async saveSchedule(value) {
    await fs.mkdir(this.root, {recursive: true, mode: 0o700});
    const target = path.join(this.root, 'automatic.json'), temporary = target + '.' + crypto.randomUUID();
    await fs.writeFile(temporary, JSON.stringify(value) + '\n', {mode: 0o600, flag: 'wx'});
    try { await fs.rename(temporary, target); } finally { await fs.rm(temporary, {force: true}); }
  }
  // The daemon's memory as an unencrypted archive in a private folder.
  async archive(id, work) {
    const {row} = await this.instances.daemonContext(id);
    const result = await this.backend.daemonSettings({action: 'backup', vault: row.daemon.memory, spool: row.healthPaths?.spool || null,
      destination: path.join(work, 'memory.zip')}, 'backup');
    return {row, result, plain: path.join(work, 'memory.zip')};
  }
  async toFile(id, password, destination) {
    checkedPassword(password);
    const work = await this.folder('backup-');
    const encrypted = path.join(path.dirname(destination), '.' + path.basename(destination) + '.' + crypto.randomUUID() + '.tmp');
    try {
      const {result, plain} = await this.archive(id, work);
      await backupCrypto.encrypt(plain, encrypted, password);
      // The user confirmed replacing an existing file in the save dialog.
      await fs.rename(encrypted, destination);
      return {saved: true, file: path.basename(destination), files: result.files, bytes: result.bytes, failedTurns: result.failed_turns};
    } finally { await fs.rm(work, {recursive: true, force: true}); await fs.rm(encrypted, {force: true}); }
  }
  async toDrive(id, {password = null, automatic = false, signal} = {}) {
    if (!this.drive) throw new Error('google.notConfigured');
    const work = await this.folder('drive-');
    try {
      const {row, result, plain} = await this.archive(id, work);
      const encrypted = path.join(work, 'memory.' + EXTENSION);
      if (automatic) await backupCrypto.encryptWithKey(plain, encrypted, await this.keyStore(row.daemon.id).get());
      else await backupCrypto.encrypt(plain, encrypted, checkedPassword(password));
      await fs.rm(plain, {force: true});
      const label = {kind: automatic ? 'automatic' : 'manual', daemon: row.daemon.id, name: row.daemon.name};
      const uploaded = await this.drive.upload(encrypted, {signal, label});
      if (automatic) await this.rotate(row.daemon.id, signal);
      return {saved: true, drive: true, files: result.files, bytes: uploaded.bytes, failedTurns: result.failed_turns};
    } finally { await fs.rm(work, {recursive: true, force: true}); }
  }
  // Keeps the latest automatic backups of one daemon; manual ones are never deleted.
  async rotate(daemon, signal) {
    const automatic = (await this.drive.backups(signal)).filter(b => b.kind === 'automatic' && b.daemon === daemon);
    for (const old of automatic.slice(KEEP_AUTOMATIC)) await this.drive.remove(old.id, daemon, signal);
  }
  async status(id = null) {
    const connected = this.oauth ? Boolean(await this.tokens.get().catch(() => null)) : false;
    const result = {configured: Boolean(this.oauth), connected};
    if (id !== null) {
      const {row} = await this.instances.daemonContext(id);
      const entry = (await this.schedule()).daemons[row.daemon.id] || null;
      result.automatic = entry ? {enabled: Boolean(entry.enabled), lastSuccess: entry.last_success || null, lastError: entry.last_error || null} : {enabled: false, lastSuccess: null, lastError: null};
    }
    return result;
  }
  async connect() { if (!this.oauth) throw new Error('google.notConfigured'); return this.oauth.signIn(); }
  async disconnect() {
    if (this.oauth) await this.oauth.disconnect();
    // Without a sign-in, daily backups stop; their keys go too.
    const schedule = await this.schedule();
    for (const daemon of Object.keys(schedule.daemons)) await this.keyStore(daemon).delete();
    await this.saveSchedule({schema: 1, daemons: {}});
    return {connected: false};
  }
  async enableAutomatic(id, password) {
    checkedPassword(password);
    if (!this.drive || !await this.tokens.get().catch(() => null)) throw new Error('google.signInAgain');
    const {row} = await this.instances.daemonContext(id);
    await this.keyStore(row.daemon.id).put(await backupCrypto.deriveKey(password));
    const schedule = await this.schedule();
    schedule.daemons[row.daemon.id] = {...schedule.daemons[row.daemon.id], enabled: true, instance: id, name: row.daemon.name};
    await this.saveSchedule(schedule);
    return this.status(id);
  }
  async disableAutomatic(id) {
    const {row} = await this.instances.daemonContext(id);
    await this.keyStore(row.daemon.id).delete();
    const schedule = await this.schedule();
    if (schedule.daemons[row.daemon.id]) schedule.daemons[row.daemon.id].enabled = false;
    await this.saveSchedule(schedule);
    return this.status(id);
  }
  // Runs every daily backup that is due. A busy app or daemon simply waits for
  // the next check; failures are recorded for the daemon's page, never shown as alerts.
  async runDue() {
    if (this.running || !this.drive) return this.running;
    this.running = (async () => {
      const schedule = await this.schedule();
      for (const [daemon, entry] of Object.entries(schedule.daemons)) {
        if (!entry.enabled || (entry.last_success && this.now() - Date.parse(entry.last_success) < DAY)) continue;
        if (entry.last_error && entry.last_attempt && this.now() - Date.parse(entry.last_attempt) < RETRY_AFTER_FAILURE) continue;
        try {
          await this.toDrive(entry.instance, {automatic: true});
          Object.assign(entry, {last_success: new Date(this.now()).toISOString(), last_error: null});
        } catch (error) {
          const code = String(error.message || '');
          if (/busy|already running|Finish or stop/i.test(code)) continue;
          entry.last_error = /^(backup|archive|google|drive)\.[a-zA-Z-]+$/.test(code) ? code : 'backup.unavailable';
          entry.last_attempt = new Date(this.now()).toISOString();
        }
        await this.saveSchedule(schedule);
      }
    })().finally(() => { this.running = null; });
    return this.running;
  }
  async list(signal) { if (!this.drive) throw new Error('google.notConfigured'); return this.drive.backups(signal); }
  async download(fileId, signal) {
    if (!this.drive) throw new Error('google.notConfigured');
    const work = await this.folder('download-'), target = path.join(work, 'backup.' + EXTENSION);
    await this.drive.download(fileId, target, {signal});
    return target;
  }
}
module.exports = {Backups, checkedPassword, fileName, EXTENSION, KEEP_AUTOMATIC, RETRY_AFTER_FAILURE};
