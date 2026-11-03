'use strict';
// App updates. The channel document names one release; every downloaded file
// must match its SHA-256, and the release's own install script replaces the
// app after it quits. That script keeps the previous app when anything fails.
const crypto = require('node:crypto');
const fsp = require('node:fs/promises');
const path = require('node:path');

// The one place that names where updates come from. A move to another
// repository ships as a last release on this channel built with the new value.
const SOURCE = Object.freeze({repository: 'jpacifico/daemon-harness',
  path: 'apps/companion/distribution/channel.json',
  refs: Object.freeze({main: 'main', candidate: 'companion-candidate'})});
const SCHEMA = 'daemon.companion.channel.v1';
const PLATFORMS = Object.freeze({
  'darwin-arm64': Object.freeze({key: 'macos-arm64', package: 'Daemons-Companion-macOS-arm64.dmg',
    installer: 'install-macos.sh', helper: 'apply-macos.sh'}),
  'win32-x64': Object.freeze({key: 'windows-x64', package: 'Daemons-Companion-Windows-x64.exe',
    installer: 'install-windows.ps1', helper: 'apply-windows.ps1'}),
});
const MAX_PACKAGE = 512 * 1024 * 1024, MAX_INSTALLER = 65536, MAX_CHANNEL = 16384;
const CHECK_DELAY = 20000, CHECK_INTERVAL = 6 * 3600 * 1000;
const DOWNLOAD_TIMEOUT = 30 * 60 * 1000;
const ERRORS = new Set(['update.access', 'update.unreachable', 'update.invalid', 'update.verify', 'update.failed', 'update.busy']);

class UpdateError extends Error {}
const fail = code => { throw new UpdateError(code); };
const codeOf = error => error instanceof UpdateError && ERRORS.has(error.message) ? error.message : 'update.failed';
// What a report keeps of a failure: its code and a short technical cause.
const failure = (stage, error) => ({stage, code: codeOf(error), at: new Date().toISOString(),
  detail: error instanceof UpdateError ? null : String(error?.message || error).slice(0, 400)});

function parseChannel(text, channel) {
  if (typeof text !== 'string' || text.length > MAX_CHANNEL) fail('update.invalid');
  let doc;
  try { doc = JSON.parse(text); } catch { fail('update.invalid'); }
  const file = (entry, name, max) => entry && entry.name === name && /^[a-f0-9]{64}$/.test(entry.sha256) &&
    Number.isSafeInteger(entry.size) && entry.size > 0 && entry.size <= max;
  if (!doc || doc.schema !== SCHEMA || doc.channel !== channel || typeof doc.release !== 'string' ||
      !/^companion-[A-Za-z0-9._-]{1,80}$/.test(doc.release) || !/^[a-f0-9]{40}$/.test(doc.source_revision || '') ||
      !doc.platforms || typeof doc.platforms !== 'object') fail('update.invalid');
  const platforms = {};
  for (const platform of Object.values(PLATFORMS)) {
    const entry = doc.platforms[platform.key];
    if (!entry || !file(entry.package, platform.package, MAX_PACKAGE) || !file(entry.installer, platform.installer, MAX_INSTALLER)) fail('update.invalid');
    platforms[platform.key] = {package: {name: entry.package.name, sha256: entry.package.sha256, size: entry.package.size},
      installer: {name: entry.installer.name, sha256: entry.installer.sha256, size: entry.installer.size}};
  }
  return {channel, release: doc.release, revision: doc.source_revision, platforms};
}

// The install script carries its own release and package digest; both must
// agree with the channel before the app hands it the package.
function checkInstaller(text, platform, release, digest) {
  const patterns = platform.installer.endsWith('.sh')
    ? [/^\s*daemon_tag=([^\s]+)$/gm, /^\s*daemon_sha=([a-f0-9]{64})$/gm]
    : [/^\s*\$daemonTag = '([^']+)'$/gm, /^\s*\$daemonDigest = '([a-f0-9]{64})'$/gm];
  const values = patterns.map(pattern => [...text.matchAll(pattern)].map(match => match[1]));
  if (values.some(found => found.length !== 1) || values[0][0] !== release || values[1][0] !== digest) fail('update.verify');
}

// Where the running app is installed, or null when it cannot replace itself
// (a source run, a mounted disk image, or a translocated copy).
function appLocation(platform, execPath) {
  if (platform === 'darwin') {
    const bundle = path.posix.resolve(execPath, '../../..');
    if (path.posix.basename(bundle) !== 'Daemons Companion.app' || bundle.startsWith('/Volumes/') || bundle.includes('/AppTranslocation/')) return null;
    return path.posix.dirname(bundle);
  }
  if (platform === 'win32') return path.win32.basename(execPath) === 'Daemons Companion.exe' ? path.win32.dirname(execPath) : null;
  return null;
}

// GitHub CLI serves private releases. Apps opened from the Finder or Start do
// not inherit the shell PATH, so the usual install folders are searched too.
function ghCandidates(platform, env) {
  const win = platform === 'win32', paths = win ? path.win32 : path.posix;
  const dirs = String(env.PATH || env.Path || '').split(win ? ';' : ':');
  if (win) dirs.push(env.ProgramFiles && paths.join(env.ProgramFiles, 'GitHub CLI'),
    env.LOCALAPPDATA && paths.join(env.LOCALAPPDATA, 'Microsoft', 'WinGet', 'Links'));
  else dirs.push('/opt/homebrew/bin', '/usr/local/bin', env.HOME && paths.join(env.HOME, '.local', 'bin'));
  return [...new Set(dirs.filter(dir => dir && paths.isAbsolute(dir)).map(dir => paths.join(dir, win ? 'gh.exe' : 'gh')))];
}

class Source {
  constructor({fetch, gh = null}) { this.fetch = fetch; this.gh = gh; }
  async text(ref) {
    let status = 0;
    try {
      const response = await this.fetch(`https://raw.githubusercontent.com/${SOURCE.repository}/${ref}/${SOURCE.path}`,
        {signal: AbortSignal.timeout(30000), cache: 'no-store'});
      status = response.status;
      if (response.ok) return await response.text();
    } catch { /* GitHub CLI below, or unreachable. */ }
    const gh = await this.gh?.();
    if (!gh) fail(status === 404 ? 'update.access' : 'update.unreachable');
    try {
      return await gh(['api', `repos/${SOURCE.repository}/contents/${SOURCE.path}?ref=${ref}`, '-H', 'Accept: application/vnd.github.raw+json'], 30000);
    } catch { fail('update.access'); }
  }
  async download(release, file, target, progress) {
    let received = 0;
    const check = () => { if (received > file.size) fail('update.verify'); progress(Math.min(received, file.size)); };
    const handle = await fsp.open(target, 'wx', 0o600);
    let response = null;
    try {
      try {
        response = await this.fetch(`https://github.com/${SOURCE.repository}/releases/download/${release}/${file.name}`,
          {signal: AbortSignal.timeout(DOWNLOAD_TIMEOUT)});
      } catch { response = null; }
      if (response?.ok) {
        try {
          const reader = response.body.getReader();
          for (;;) {
            const {done, value} = await reader.read();
            if (done) break;
            received += value.length; check();
            await handle.write(value);
          }
        } catch (error) { if (error instanceof UpdateError) throw error; fail('update.unreachable'); }
      }
    } finally { await handle.close(); }
    if (!response?.ok) {
      const gh = await this.gh?.();
      if (!gh) fail(response?.status === 404 ? 'update.access' : 'update.unreachable');
      await fsp.rm(target, {force: true});
      const timer = setInterval(() => fsp.stat(target).then(info => { received = info.size; progress(Math.min(received, file.size)); }, () => {}), 500);
      try { await gh(['release', 'download', release, '--repo', SOURCE.repository, '--pattern', file.name, '--output', target], DOWNLOAD_TIMEOUT); }
      catch { fail('update.access'); }
      finally { clearInterval(timer); }
    }
    const info = await fsp.lstat(target);
    if (!info.isFile() || info.size !== file.size) fail('update.verify');
    const hash = crypto.createHash('sha256');
    for await (const chunk of require('node:fs').createReadStream(target)) hash.update(chunk);
    if (hash.digest('hex') !== file.sha256) fail('update.verify');
  }
}

class Updates {
  constructor({channel, revision, platform, arch, execPath, pid, root, helpers, packaged, fetch, gh = null,
    launch, emit = () => {}, writable = () => true, env = {}}) {
    this.platform = PLATFORMS[platform + '-' + arch] || null;
    this.channel = Object.hasOwn(SOURCE.refs, channel) ? channel : null;
    this.revision = /^[a-f0-9]{40}$/.test(revision || '') ? revision : null;
    this.destination = appLocation(platform, execPath);
    this.enabled = Boolean(packaged && this.platform && this.channel && this.revision && this.destination && writable(this.destination));
    Object.assign(this, {platformName: platform, pid, root, helpers, launch, emit, env});
    this.source = new Source({fetch, gh});
    this.state = this.enabled ? 'idle' : 'disabled';
    this.release = null; this.progress = null; this.error = null; this.notice = null; this.failure = null; this.timers = [];
  }
  status() {
    return {enabled: this.enabled, channel: this.channel, state: this.state, release: this.release?.release || null,
      progress: this.progress, error: this.error, notice: this.notice};
  }
  changed() { this.emit(this.status()); }
  // A previous attempt left a marker: the revision now running tells whether it worked.
  async startup() {
    if (!this.enabled) return;
    let attempt = null;
    try { attempt = JSON.parse(await fsp.readFile(path.join(this.root, 'attempt.json'), 'utf8')); } catch { /* none */ }
    if (attempt && /^[a-f0-9]{40}$/.test(attempt.revision || '')) {
      this.notice = attempt.revision === this.revision ? 'installed' : 'failed';
      await fsp.copyFile(path.join(this.root, 'staging', 'install.log'), path.join(this.root, 'last-install.log')).catch(() => {});
    }
    await fsp.rm(path.join(this.root, 'staging'), {recursive: true, force: true}).catch(() => {});
    await fsp.rm(path.join(this.root, 'attempt.json'), {force: true}).catch(() => {});
  }
  schedule() {
    if (!this.enabled) return;
    const run = () => this.check().catch(() => {});
    this.timers.push(setTimeout(run, CHECK_DELAY), setInterval(run, CHECK_INTERVAL));
    for (const timer of this.timers) timer.unref?.();
  }
  // A report the user can export: states and the helper's log, without private paths.
  async diagnostic() {
    const read = async name => {
      try { return (await fsp.readFile(path.join(this.root, name), 'utf8')).slice(-12000); } catch { return null; }
    };
    const home = this.env.USERPROFILE || this.env.HOME || '';
    const clean = value => typeof value === 'string' && home ? value.split(home).join('~') : value;
    return {schema: 'daemon.app-update-diagnostic.v1', at: new Date().toISOString(),
      platform: this.platformName, revision: this.revision, ...this.status(),
      failure: this.failure ? {...this.failure, detail: clean(this.failure.detail)} : null,
      install_log: clean(await read(path.join('staging', 'install.log')) ?? await read('last-install.log'))};
  }
  dismiss() { this.notice = null; this.changed(); return this.status(); }
  async check({manual = false} = {}) {
    if (!this.enabled || ['checking', 'downloading', 'installing'].includes(this.state)) return this.status();
    const previous = this.state;
    this.state = 'checking'; this.error = null; this.changed();
    try {
      const release = parseChannel(await this.source.text(SOURCE.refs[this.channel]), this.channel);
      this.release = release.revision === this.revision ? null : release;
      this.state = this.release ? 'available' : 'current';
    } catch (error) {
      // A background check that fails keeps what the user already saw.
      this.state = manual || !['available', 'current'].includes(previous) ? 'failed' : previous;
      if (manual) this.error = codeOf(error);
      this.failure = failure('check', error);
    }
    this.changed();
    return this.status();
  }
  // Download and verify everything first; nothing is replaced in this step.
  async prepare() {
    if (!this.enabled || this.state !== 'available' || !this.release) return false;
    const files = this.release.platforms[this.platform.key];
    const staging = path.join(this.root, 'staging');
    this.state = 'downloading'; this.progress = 0; this.error = null; this.changed();
    try {
      await fsp.rm(staging, {recursive: true, force: true});
      await fsp.mkdir(staging, {recursive: true, mode: 0o700});
      const installer = path.join(staging, files.installer.name), pkg = path.join(staging, files.package.name);
      await this.source.download(this.release.release, files.installer, installer, () => {});
      checkInstaller(await fsp.readFile(installer, 'utf8'), this.platform, this.release.release, files.package.sha256);
      let last = -1;
      await this.source.download(this.release.release, files.package, pkg, received => {
        const value = Math.floor(received * 100 / files.package.size);
        if (value !== last) { last = value; this.progress = value; this.changed(); }
      });
      // Read, not copied: the helper lives inside the app archive.
      this.progress = 100; this.changed();
      await fsp.writeFile(path.join(staging, this.platform.helper), await fsp.readFile(path.join(this.helpers, this.platform.helper)), {mode: 0o700});
      this.prepared = {staging, installer, package: pkg, helper: path.join(staging, this.platform.helper), log: path.join(staging, 'install.log')};
      return true;
    } catch (error) {
      await fsp.rm(staging, {recursive: true, force: true}).catch(() => {});
      // The release stays offered, with the reason this attempt stopped.
      this.state = 'available'; this.error = codeOf(error); this.progress = null;
      this.failure = failure('download', error); this.changed();
      return false;
    }
  }
  // Starts the helper. The caller quits the app right after a true result.
  async start() {
    const p = this.prepared;
    if (!p) return false;
    await fsp.writeFile(path.join(this.root, 'attempt.json'),
      JSON.stringify({release: this.release.release, revision: this.release.revision, from: this.revision}) + '\n', {mode: 0o600});
    if (this.platformName === 'darwin') this.launch('/bin/sh', [p.helper, String(this.pid), p.installer, p.package, this.destination, p.log], {detached: true});
    else {
      // Attached, and awaited: the Windows helper hands off to a copy of itself
      // outside the app's job, then exits.
      const code = await this.launch(path.win32.join(this.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'), ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
        '-File', p.helper, '-ProcessId', String(this.pid), '-Installer', p.installer, '-Package', p.package, '-Destination', this.destination, '-Log', p.log], {detached: false});
      if (code !== 0) {
        await fsp.rm(path.join(this.root, 'attempt.json'), {force: true}).catch(() => {});
        this.failure = failure('start', new Error(`helper exit ${code}`));
        this.abandon(); this.error = 'update.failed'; this.changed();
        return false;
      }
    }
    this.state = 'installing'; this.progress = null; this.changed();
    return true;
  }
  // Prepared files stay unused until the next attempt replaces them.
  abandon() { this.prepared = null; this.state = this.release ? 'available' : 'idle'; this.progress = null; this.changed(); }
  stop() { for (const timer of this.timers) { clearTimeout(timer); clearInterval(timer); } this.timers = []; }
}

module.exports = {SOURCE, SCHEMA, PLATFORMS, Updates, Source, parseChannel, checkInstaller, appLocation, ghCandidates};
