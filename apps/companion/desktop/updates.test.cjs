'use strict';
// App updates against synthetic channels and releases; no network, no install.
const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {SOURCE, PLATFORMS, Updates, parseChannel, checkInstaller, appLocation, ghCandidates} = require('./updates.cjs');

const OLD = 'a'.repeat(40), NEW = 'b'.repeat(40), RELEASE = 'companion-candidate-2026-10-26-c20';
const sha = bytes => crypto.createHash('sha256').update(bytes).digest('hex');

function release({revision = NEW, tamper = null} = {}) {
  const files = {}, platforms = {};
  for (const platform of Object.values(PLATFORMS)) {
    const pkg = Buffer.from('package for ' + platform.key);
    const script = platform.installer.endsWith('.sh')
      ? `daemon_tag=${RELEASE}\ndaemon_sha=${sha(pkg)}\n`
      : `$daemonTag = '${RELEASE}'\n$daemonDigest = '${sha(pkg)}'\n`;
    files[platform.package] = pkg; files[platform.installer] = Buffer.from(script);
    platforms[platform.key] = {package: {name: platform.package, sha256: sha(pkg), size: pkg.length},
      installer: {name: platform.installer, sha256: sha(files[platform.installer]), size: files[platform.installer].length}};
  }
  if (tamper) files[tamper] = Buffer.from('tampered ' + tamper);
  const channel = JSON.stringify({schema: 'daemon.companion.channel.v1', channel: 'candidate', release: RELEASE, source_revision: revision, platforms});
  return {files, channel};
}
const channelUrl = `https://raw.githubusercontent.com/${SOURCE.repository}/companion-candidate/${SOURCE.path}`;
function publicFetch(served, seen = []) {
  return async url => {
    seen.push(url);
    if (url === channelUrl) return new Response(served.channel);
    const name = url.startsWith(`https://github.com/${SOURCE.repository}/releases/download/${RELEASE}/`) && url.split('/').pop();
    return name && served.files[name] ? new Response(served.files[name]) : new Response('', {status: 404});
  };
}
function updates(options = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'daemons-updates-test-'));
  const launched = [], modes = [], events = [];
  const value = new Updates({channel: 'candidate', revision: OLD, platform: 'darwin', arch: 'arm64', pid: 4242,
    execPath: '/Users/test/Applications/Daemons Companion.app/Contents/MacOS/Daemons Companion',
    root, helpers: path.join(__dirname, 'updater'), packaged: true, fetch: publicFetch(release()),
    launch: async (command, args, mode) => { launched.push([command, ...args]); modes.push(mode); return 0; },
    emit: status => events.push(status), ...options});
  return {value, root, launched, modes, events};
}

test('the channel document is accepted only when complete and exact', () => {
  const served = release();
  assert.equal(parseChannel(served.channel, 'candidate').release, RELEASE);
  const doc = JSON.parse(served.channel);
  const broken = [
    {...doc, channel: 'main'},
    {...doc, schema: 'daemon.companion.channel.v0'},
    {...doc, release: '../escape'},
    {...doc, source_revision: 'short'},
    {...doc, platforms: {'macos-arm64': doc.platforms['macos-arm64']}},
    {...doc, platforms: {...doc.platforms, 'macos-arm64': {...doc.platforms['macos-arm64'], package: {...doc.platforms['macos-arm64'].package, name: 'other.dmg'}}}},
    {...doc, platforms: {...doc.platforms, 'windows-x64': {...doc.platforms['windows-x64'], package: {...doc.platforms['windows-x64'].package, size: 600 * 1024 * 1024}}}},
  ];
  for (const value of broken) assert.throws(() => parseChannel(JSON.stringify(value), 'candidate'), /update\.invalid/);
  assert.throws(() => parseChannel('{not json', 'candidate'), /update\.invalid/);
});

test('an install script must name the same release and package digest', () => {
  const mac = PLATFORMS['darwin-arm64'], win = PLATFORMS['win32-x64'];
  checkInstaller(`  daemon_tag=${RELEASE}\n  daemon_sha=${'c'.repeat(64)}\n`, mac, RELEASE, 'c'.repeat(64));
  checkInstaller(`    $daemonTag = '${RELEASE}'\n    $daemonDigest = '${'c'.repeat(64)}'\n`, win, RELEASE, 'c'.repeat(64));
  assert.throws(() => checkInstaller(`daemon_tag=${RELEASE}\ndaemon_sha=${'d'.repeat(64)}\n`, mac, RELEASE, 'c'.repeat(64)), /update\.verify/);
  assert.throws(() => checkInstaller(`daemon_tag=other\ndaemon_sha=${'c'.repeat(64)}\n`, mac, RELEASE, 'c'.repeat(64)), /update\.verify/);
  assert.throws(() => checkInstaller(`daemon_tag=${RELEASE}\ndaemon_sha=${'c'.repeat(64)}\ndaemon_sha=${'c'.repeat(64)}\n`, mac, RELEASE, 'c'.repeat(64)), /update\.verify/);
});

test('only an installed app can replace itself', () => {
  assert.equal(appLocation('darwin', '/Applications/Daemons Companion.app/Contents/MacOS/Daemons Companion'), '/Applications');
  assert.equal(appLocation('darwin', '/Volumes/Daemons Companion/Daemons Companion.app/Contents/MacOS/Daemons Companion'), null);
  assert.equal(appLocation('darwin', '/private/var/folders/x/AppTranslocation/y/d/Daemons Companion.app/Contents/MacOS/Daemons Companion'), null);
  assert.equal(appLocation('darwin', '/repo/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron'), null);
  assert.equal(appLocation('win32', 'C:\\Users\\Alice\\AppData\\Local\\Programs\\Daemons Companion\\Daemons Companion.exe'),
    'C:\\Users\\Alice\\AppData\\Local\\Programs\\Daemons Companion');
  assert.equal(appLocation('win32', 'C:\\repo\\node_modules\\electron\\dist\\electron.exe'), null);
  assert.equal(appLocation('linux', '/opt/Daemons Companion/daemons-companion'), null);
});

test('GitHub CLI is searched beyond the PATH a desktop app inherits', () => {
  assert.ok(ghCandidates('darwin', {PATH: '/usr/bin:/bin', HOME: '/Users/test'}).includes('/opt/homebrew/bin/gh'));
  const windows = ghCandidates('win32', {Path: 'C:\\Windows\\System32;relative', ProgramFiles: 'C:\\Program Files', LOCALAPPDATA: 'C:\\Users\\T\\AppData\\Local'});
  assert.ok(windows.includes('C:\\Program Files\\GitHub CLI\\gh.exe'));
  assert.ok(windows.includes('C:\\Users\\T\\AppData\\Local\\Microsoft\\WinGet\\Links\\gh.exe'));
  assert.ok(!windows.some(candidate => candidate.startsWith('relative')));
});

test('updates stay off for source runs, other platforms and unknown channels', () => {
  for (const options of [{packaged: false}, {platform: 'linux'}, {arch: 'x64'}, {channel: 'nightly'}, {revision: 'unknown'}, {writable: () => false}]) {
    const {value} = updates(options);
    assert.equal(value.status().state, 'disabled');
    assert.equal(value.status().enabled, false);
  }
});

test('a newer public release is verified, staged and handed to the detached helper', async () => {
  const {value, root, launched, modes, events} = updates();
  assert.equal((await value.check()).state, 'available');
  assert.equal(await value.prepare(), true);
  assert.ok(events.some(status => status.state === 'downloading' && status.progress === 100));
  assert.equal(await value.start(), true);
  assert.equal(value.status().state, 'installing');
  const staging = path.join(root, 'staging');
  assert.deepEqual(launched, [['/bin/sh', path.join(staging, 'apply-macos.sh'), '4242', path.join(staging, 'install-macos.sh'),
    path.join(staging, 'Daemons-Companion-macOS-arm64.dmg'), '/Users/test/Applications', path.join(staging, 'install.log')]]);
  assert.deepEqual(modes, [{detached: true}]);
  assert.equal(fs.readFileSync(path.join(staging, 'apply-macos.sh'), 'utf8'), fs.readFileSync(path.join(__dirname, 'updater', 'apply-macos.sh'), 'utf8'));
  assert.equal(JSON.parse(fs.readFileSync(path.join(root, 'attempt.json'), 'utf8')).revision, NEW);

  // The next start reads the revision it runs: the same one means installed.
  fs.writeFileSync(path.join(staging, 'install.log'), 'exit=0\n');
  const after = updates({root, revision: NEW}).value;
  await after.startup();
  assert.equal(after.status().notice, 'installed');
  assert.equal(fs.existsSync(path.join(root, 'attempt.json')), false);
  assert.equal(fs.existsSync(staging), false);
  assert.equal(fs.readFileSync(path.join(root, 'last-install.log'), 'utf8'), 'exit=0\n');
  assert.equal(after.dismiss().notice, null);
});

test('an app still on its old revision after an attempt reports that the update failed', async () => {
  const {value, root} = updates();
  await value.check(); await value.prepare(); await value.start();
  const after = updates({root}).value;
  await after.startup();
  assert.equal(after.status().notice, 'failed');
});

const windows = {platform: 'win32', arch: 'x64', env: {SystemRoot: 'C:\\Windows'},
  execPath: 'C:\\Users\\T\\AppData\\Local\\Programs\\Daemons Companion\\Daemons Companion.exe'};

test('Windows hands the helper the app folder and the system PowerShell, attached and awaited', async () => {
  const {value, root, launched, modes} = updates(windows);
  await value.check(); assert.equal(await value.prepare(), true);
  assert.equal(await value.start(), true);
  assert.equal(value.status().state, 'installing');
  const staging = path.join(root, 'staging');
  assert.deepEqual(launched[0], ['C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe', '-NoProfile', '-NonInteractive',
    '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', path.join(staging, 'apply-windows.ps1'), '-ProcessId', '4242',
    '-Installer', path.join(staging, 'install-windows.ps1'), '-Package', path.join(staging, 'Daemons-Companion-Windows-x64.exe'),
    '-Destination', 'C:\\Users\\T\\AppData\\Local\\Programs\\Daemons Companion', '-Log', path.join(staging, 'install.log')]);
  // Electron kills its attached children when it quits, and a detached
  // PowerShell never runs: the helper must be attached, then hand off.
  assert.deepEqual(modes, [{detached: false}]);
});

test('a Windows helper that cannot hand off keeps the app open and the release offered', async () => {
  for (const code of [1, null]) {
    const {value, root} = updates({...windows, launch: async () => code});
    await value.check(); assert.equal(await value.prepare(), true);
    assert.equal(await value.start(), false);
    assert.equal(value.status().state, 'available');
    assert.equal(value.status().error, 'update.failed');
    assert.equal(fs.existsSync(path.join(root, 'attempt.json')), false);
  }
});

test('the running revision is current, and nothing is downloaded', async () => {
  const seen = [];
  const {value} = updates({revision: NEW, fetch: publicFetch(release(), seen)});
  assert.equal((await value.check()).state, 'current');
  assert.equal(await value.prepare(), false);
  assert.deepEqual(seen, [channelUrl]);
});

for (const tamper of ['Daemons-Companion-macOS-arm64.dmg', 'install-macos.sh']) {
  test(`a changed ${tamper} is refused before anything is replaced`, async () => {
    const {value, root, launched} = updates({fetch: publicFetch(release({tamper}))});
    await value.check();
    assert.equal(await value.prepare(), false);
    assert.equal(value.status().state, 'available');
    assert.equal(value.status().error, 'update.verify');
    assert.equal(await value.start(), false);
    assert.deepEqual(launched, []);
    assert.equal(fs.existsSync(path.join(root, 'staging')), false);
    assert.equal(fs.existsSync(path.join(root, 'attempt.json')), false);
  });
}

test('a private repository is read through GitHub CLI', async () => {
  const served = release(), calls = [];
  const gh = async () => async args => {
    calls.push(args[0]);
    if (args[0] === 'api') {
      assert.deepEqual(args, ['api', `repos/${SOURCE.repository}/contents/${SOURCE.path}?ref=companion-candidate`, '-H', 'Accept: application/vnd.github.raw+json']);
      return served.channel;
    }
    assert.deepEqual(args.slice(0, 4), ['release', 'download', RELEASE, '--repo']);
    fs.writeFileSync(args[args.indexOf('--output') + 1], served.files[args[args.indexOf('--pattern') + 1]]);
    return '';
  };
  const {value} = updates({fetch: async () => new Response('', {status: 404}), gh});
  assert.equal((await value.check()).state, 'available');
  assert.equal(await value.prepare(), true);
  assert.deepEqual(calls, ['api', 'release', 'release']);
});

test('without access, a manual check says how to sign in; a background one keeps what was shown', async () => {
  const {value} = updates({fetch: async () => new Response('', {status: 404})});
  assert.equal((await value.check({manual: true})).error, 'update.access');
  const offline = updates({fetch: async () => { throw new TypeError('offline'); }}).value;
  assert.equal((await offline.check({manual: true})).error, 'update.unreachable');

  let online = true;
  const flaky = updates({fetch: async url => { if (!online) throw new TypeError('offline'); return publicFetch(release())(url); }}).value;
  assert.equal((await flaky.check()).state, 'available');
  online = false;
  assert.equal((await flaky.check()).state, 'available');
  assert.equal(flaky.status().error, null);
});

test('a download larger than published stops while it is received', async () => {
  const served = release();
  served.files['Daemons-Companion-macOS-arm64.dmg'] = Buffer.alloc(4096, 1);
  const {value} = updates({fetch: publicFetch(served)});
  await value.check();
  assert.equal(await value.prepare(), false);
  assert.equal(value.status().error, 'update.verify');
});
