'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { Backend, verifyPackage, cleanEnvironment, allowedLink, PYTHON_FLAGS } = require('./backend.cjs');
const digest = data => crypto.createHash('sha256').update(data).digest('hex');
test('package verified before execution; extra and altered files refused', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'desktop-package-'));
  try {
    await fs.mkdir(path.join(root,'bin'));
    const target = process.platform === 'win32' ? 'windows' : 'macos';
    const name = target === 'windows' ? 'uv.exe' : 'uv';
    const file = path.join(root,'bin',name);
    await fs.writeFile(file,'synthetic',{ mode: 0o755 });
    const manifest = JSON.stringify({ schema:'daemon.companion-package.v1', files: {
      ['bin/'+name]: {sha256:digest('synthetic'),size:9,executable:true}
    }});
    await fs.writeFile(path.join(root,'companion-manifest.json'),manifest);
    const metadata = {manifest_sha256:digest(manifest),uv_sha256:digest('synthetic')};
    await verifyPackage(root, metadata, target);
    await fs.writeFile(path.join(root,'extra'),'x');
    await assert.rejects(verifyPackage(root,metadata,target));
    await fs.unlink(path.join(root,'extra'));
    await fs.writeFile(file,'changed!!');
    await assert.rejects(verifyPackage(root,metadata,target));
  } finally { await fs.rm(root,{recursive:true,force:true}); }
});
test('no inherited Python or host configuration', () => {
  assert.deepEqual(cleanEnvironment({PATH:'p',PYTHONHOME:'bad',UV_INDEX_URL:'bad',CODEX_HOME:'bad',HERMES_HOME:'bad',DAEMON_CORE_MEMORY_ROOT:'bad',NODE_OPTIONS:'bad'}),{PATH:'p'});
});
test('answers consume only the matching prompt; secrets not emitted', () => {
  const events=[], frames=[];
  const backend = new Backend({emit:event=>events.push(event)});
  backend.active=true; backend.child={stdin:{write:line=>frames.push(JSON.parse(line))}};
  backend.prompt={id:2,kind:'secret',maximum:100};
  assert.throws(()=>backend.answer({id:1,action:'submit',value:'private'}));
  backend.answer({id:2,action:'submit',value:'private'});
  assert.throws(()=>backend.answer({id:2,action:'submit',value:'private'}));
  assert.equal(frames.length,1); assert.equal(events.length,0);
});
test('browser URLs remain bounded and approved', () => {
  assert.ok(allowedLink('https://auth.openai.com/authorize?state=synthetic'));
  for (const link of ['file:///tmp/x','https://auth.openai.com.evil.test/','https://x@auth.openai.com/','https://t.me:8080/']) assert.equal(allowedLink(link),false);
});

test('attachment child preserves bounded diagnostics across the real subprocess transport',async()=>{
 const {settingsCommand}=require('./backend.cjs');
 const reply={ok:false,code:'source-environment',diagnostic:{attachment_stage:'source-environment',exception_type:'RuntimeError',failure_reason:'environment-content-changed',errno:2,winerror:206,message:'private-secret'}};
 await assert.rejects(settingsCommand(process.execPath,['-e',`process.stdin.resume();process.stdin.on('end',()=>process.stdout.write(${JSON.stringify(JSON.stringify(reply))}))`],{}, {operation:'create-start'},'attachment'),error=>{
  assert.equal(error.message,'attachment.source-environment');assert.equal(error.attachmentDiagnostic.winerror,206);assert.equal(error.attachmentDiagnostic.failure_reason,'environment-content-changed');assert(!JSON.stringify(error).includes('private-secret'));return true;
 });
 await assert.rejects(settingsCommand(process.execPath,['-e','process.stdin.resume();process.stdin.on("end",()=>process.exit(3))'],{},{},'attachment'),error=>error.attachmentDiagnostic.exit_code===3&&error.attachmentDiagnostic.attachment_stage==='transport');
});

test('native ChatGPT events are ephemeral, bounded and separate from diagnostics',async()=>{
 const {settingsCommand}=require('./backend.cjs');const events=[];
 const lines=[{code:'TEST-0000'},{url:'https://auth.openai.com/codex/device'},{url:'https://evil.invalid/'},{access:'secret'},{code:'not a code'},{code:'TEST-0000',refresh:'secret'}].map(v=>'DAEMONS_CHATGPT '+JSON.stringify(v)+'\n').join('');
 const childCode=`process.stdin.resume();process.stdin.on('end',()=>{process.stderr.write(${JSON.stringify(lines)});process.stdout.write(JSON.stringify({ok:true,result:{revision:'test',phase:'model'}}));});`;
 const result=await settingsCommand(process.execPath,['-e',childCode],{},{},'attachment',{child(){},event:e=>events.push(e)});
 assert.equal(result.phase,'model');assert.deepEqual(events,[{code:'TEST-0000'},{url:'https://auth.openai.com/codex/device'}]);
});

test('explicit cancellation of native sign-in is not reported as an association failure',async()=>{
 const {settingsCommand}=require('./backend.cjs');let cancelled=false;
 await assert.rejects(settingsCommand(process.execPath,['-e','setInterval(()=>{},1000)'],{},{},'attachment',{
  child:child=>setTimeout(()=>{cancelled=true;child.kill();},20),cancelled:()=>cancelled,event(){}
 }),error=>error.message==='attachment.chatgpt-cancelled');
});


test('ChatGPT progress and bounded native catalogue pass the subprocess boundary',async()=>{
 const {settingsCommand}=require('./backend.cjs');const events=[];
 const catalog={status:'ready',raw_catalog_observed:false,native_count:2,excluded_count:1,models:['gpt-6-astra','gpt-6.1-sol'].map((id,i)=>({id,status:i===1?'offered':'absent-native'}))};
 const lines=[{stage:'waiting'},{stage:'secret'},{stage:'catalog'},{catalog},{stage:'catalog',token:'secret'}].map(v=>'DAEMONS_CHATGPT '+JSON.stringify(v)+'\n').join('');
 const code=`process.stdin.resume();process.stdin.on('end',()=>{process.stderr.write(${JSON.stringify(lines)});process.stdout.write(JSON.stringify({ok:true,result:{revision:'test',phase:'model'}}));});`;
 await settingsCommand(process.execPath,['-e',code],{},{},'attachment',{child(){},event:e=>events.push(e)});
 assert.deepEqual(events,[{stage:'waiting'},{stage:'catalog'},{catalog}]);
});

test('isolated Python still reads accented answers as UTF-8', () => {
  // -I ignores PYTHONUTF8; without -X utf8 Windows decodes stdin with its code page.
  assert.deepEqual(PYTHON_FLAGS.slice(0, 3), ['-I', '-X', 'utf8']);
  const { spawnSync } = require('node:child_process');
  const python = process.platform === 'win32' ? 'python' : 'python3';
  const result = spawnSync(python, [...PYTHON_FLAGS, '-c',
    'import sys;line=sys.stdin.readline().strip();print(sys.stdin.encoding, line == "Aur\\u00e9lie")'],
    { input: 'Aurélie\n', env: { ...process.env, PYTHONIOENCODING: 'latin-1', LC_ALL: 'C' }, encoding: 'utf8' });
  if (result.error) return;  // no system Python on this machine
  assert.equal(result.stdout.trim(), 'utf-8 True');
});

test('a backup archive survives its caller removing the decrypted copy right after start', async () => {
  // 2026-10-30: the app deleted the decrypted backup as soon as start returned,
  // before the setup had moved it, so installing from a large backup stopped at once.
  const fsp = require('node:fs/promises'), os = require('node:os');
  const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'restore-race-'));
  const work = path.join(root, 'work'), home = path.join(root, 'home'), bootstrap = path.join(root, 'bootstrap');
  await fsp.mkdir(work); await fsp.mkdir(home); await fsp.mkdir(bootstrap);
  await fsp.writeFile(path.join(work, 'backup.zip'), 'synthetic archive');
  const events = [];
  const backend = new Backend({emit: event => events.push(event), bootstrapHome: bootstrap,
    setupHomes: {create: () => new Promise(resolve => setTimeout(() => resolve(home), 50))}});
  backend.prepare = async () => { throw new Error('synthetic stop after the archive move'); };
  await backend.start('new', null, null, path.join(work, 'backup.zip'));
  await fsp.rm(work, {recursive: true, force: true});
  await backend.stopped;
  assert.equal(await fsp.readFile(path.join(home, 'restore', 'backup.zip'), 'utf8'), 'synthetic archive');
  assert.deepEqual(await fsp.readdir(path.join(bootstrap, 'restore-pending')), []);
  await fsp.rm(root, {recursive: true, force: true});
});
