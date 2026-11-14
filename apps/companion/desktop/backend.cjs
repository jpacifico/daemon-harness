'use strict';
// Main-process-only transport. No shell, user-selected executable or renderer path.
const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const { StringDecoder } = require('node:string_decoder');
const {Diagnostics}=require('./diagnostics.cjs');
const SCHEMA = 'daemon.desktop.v1';
const MAX_FRAME = 65536;
const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex');

// Isolated mode (-I) ignores PYTHON* variables, so UTF-8 for the user's text
// (names with accents, on Windows) is requested on the command line.
const PYTHON_FLAGS = Object.freeze(['-I', '-X', 'utf8', '-B', '-u']);

function allowedLink(value) {
  if (typeof value !== 'string' || value.length > 8192) return false;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password && !url.port &&
      ['auth.openai.com', 'web.telegram.org', 't.me'].includes(url.hostname);
  } catch { return false; }
}

async function verifyPackage(root, expected, targetOS) {
  const bytes = await fs.readFile(path.join(root, 'companion-manifest.json'));
  if (digest(bytes) !== expected.manifest_sha256) throw new Error('The installer manifest changed. Download a fresh companion.');
  const manifest = JSON.parse(bytes);
  if (manifest.schema !== 'daemon.companion-package.v1' || !manifest.files ||
      Object.keys(manifest.files).length > 5000) throw new Error('Invalid installer manifest.');
  const actual = new Set();
  async function walk(directory) {
    const info = await fs.lstat(directory);
    if (info.isSymbolicLink() || !info.isDirectory()) throw new Error('Installer directories must be direct.');
    for (const name of await fs.readdir(directory)) {
      const file = path.join(directory, name);
      const relative = path.relative(root, file).split(path.sep).join('/');
      const stat = await fs.lstat(file);
      if (stat.isSymbolicLink()) throw new Error('Installer links are unsupported.');
      if (stat.isDirectory()) { await walk(file); continue; }
      if (relative === 'companion-manifest.json') continue;
      const entry = manifest.files[relative];
      if (!stat.isFile() || !entry || stat.size > 128 * 1024 * 1024 || stat.size !== entry.size ||
          digest(await fs.readFile(file)) !== entry.sha256 ||
          (targetOS === 'windows' ? name.toLowerCase().endsWith('.exe') : Boolean(stat.mode & 0o111)) !== entry.executable) {
        throw new Error('Installer integrity verification failed. Download a fresh companion.');
      }
      actual.add(relative);
    }
  }
  await walk(root);
  if (actual.size !== Object.keys(manifest.files).length) throw new Error('The installer is incomplete.');
  const uv = 'bin/' + (targetOS === 'windows' ? 'uv.exe' : 'uv');
  if (manifest.files[uv]?.sha256 !== expected.uv_sha256) throw new Error('The Python bootstrap changed.');
}

function cleanEnvironment(input) {
  return Object.fromEntries(Object.entries(input).filter(([key]) =>
    !/^(PYTHON|UV_|DAEMON_|LLM_|CODEX_|HERMES_|OPENCLAW_)/i.test(key) &&
    !['VIRTUAL_ENV', 'CONDA_PREFIX', 'NODE_OPTIONS', 'ELECTRON_RUN_AS_NODE'].includes(key)));
}

function command(executable, args, options, signal, timeout = 600000) {
  return new Promise((resolve, reject) => {
    const child = spawn(executable, args, { ...options, windowsHide: true, shell: false,
      stdio: ['ignore', 'pipe', 'pipe'], signal });
    let stdout = '', length = 0;
    const timer = setTimeout(() => child.kill(), timeout);
    child.stdout.on('data', bytes => {
      length += bytes.length;
      if (length > MAX_FRAME) child.kill(); else stdout += bytes.toString();
    });
    child.stderr.on('data', () => {}); // drain, never expose native credential output
    child.on('error', error => { clearTimeout(timer); reject(error); });
    child.on('close', code => {
      clearTimeout(timer);
      if (code === 0 && length <= MAX_FRAME) resolve(stdout.trim());
      else reject(new Error('The private Python component could not start. Check your connection and retry.'));
    });
  });
}

const settingsErrors = {
  unsafe_path:'The memory settings path is not safe to edit.',
  invalid_metadata:'The saved daemon settings are unreadable. No settings were changed.',
  invalid_binding:'The user account could not be identified safely.',
  invalid_values:'Check the names and pronouns. Each field accepts up to 40 characters.',
  changed:'These settings changed. Reopen the daemon settings before saving.',
  busy:'The daemon settings are busy. Please try again.',
  unsupported:'This daemon version does not support these settings.',
  unavailable:'The daemon settings could not be read or saved. Check folder access and reopen the form.',
  unconfirmed:'The save could not be confirmed. Reopen the daemon settings to check their state.'
};
function settingsCommand(executable,args,options,request,removal=false,auth=null) {
  const failure=(details={})=>{const error=new Error(removal==='attachment'?'attachment.unconfirmed':settingsErrors.unconfirmed);if(removal==='attachment')error.attachmentDiagnostic={attachment_stage:'transport',...require('./diagnostics.cjs').event(details,'installation')};return error;};
  const input=JSON.stringify(request);
  if(Buffer.byteLength(input)>32768)return Promise.reject(new Error('Invalid settings request.'));
  return new Promise((resolve,reject)=>{
    const child=spawn(executable,args,{...options,windowsHide:true,shell:false,stdio:['pipe','pipe','pipe']});
    let output=[],size=0;
    const timer=setTimeout(()=>child.kill(),removal==='attachment'?1200000:removal==='backup'?600000:removal?300000:15000);
    child.stdin.on('error',()=>{});
    if(auth){
      auth.child(child);let pending='';
      child.stderr.on('data',bytes=>{
        pending=(pending+bytes.toString('utf8')).slice(-16384);
        const lines=pending.split('\n');pending=lines.pop();
        for(const line of lines){
          if(!line.startsWith('DAEMONS_CHATGPT '))continue;
          try{const value=JSON.parse(line.slice(16));
            if(Object.keys(value).length!==1)continue;
            if(value.url==='https://auth.openai.com/codex/device'||typeof value.code==='string'&&/^[A-Z0-9-]{4,32}$/.test(value.code))auth.event(value);
            else if(require('./diagnostics.cjs').AUTH_STAGES.includes(value.stage))auth.event({stage:value.stage});
            else {const catalog=require('./diagnostics.cjs').modelCatalog(value.catalog);if(catalog)auth.event({catalog});}
          }catch{}
        }
      });
    }else child.stderr.on('data',()=>{});
    child.stdout.on('data',bytes=>{size+=bytes.length;if(size>MAX_FRAME)child.kill();else output.push(bytes);});
    child.on('error',error=>{clearTimeout(timer);reject(removal==='attachment'?failure({errno:error.errno,exception_type:'OSError'}):new Error(settingsErrors.unavailable));});
    child.on('close',code=>{
      clearTimeout(timer);
      if(auth?.cancelled?.())return reject(new Error('attachment.chatgpt-cancelled'));
      try {
        if(code!==0||size>MAX_FRAME)throw new Error();
        const result=JSON.parse(Buffer.concat(output).toString('utf8'));
        if(result.ok===false){
          const error=new Error(removal?(removal==='attachment'?'attachment.':removal==='discard'?'discard.':removal==='interface'||removal==='backup'||removal==='provider'||removal==='change'?'':'removal.')+(result.code||'unconfirmed'):settingsErrors[result.code]||settingsErrors.unavailable);
          if(removal==='attachment')error.attachmentDiagnostic=require('./diagnostics.cjs').event(result.diagnostic||{},'installation');
          if(removal===true)error.removalDiagnostic=require('./removal-diagnostics.cjs').details(result.diagnostic);
          // Backups report the exception class and error numbers only; the app shows them as a technical detail.
          if((removal==='backup'||removal==='provider'||removal==='change')&&/^[A-Za-z]{1,40}$/.test(result.diagnostic?.type||''))error.message+='|'+[result.diagnostic.type,...[result.diagnostic.errno,result.diagnostic.winerror].filter(Number.isInteger),...(/^[A-Za-z0-9_.\/-]{1,80}$/.test(result.diagnostic.area||'')?[result.diagnostic.area]:[])].join(' ');
          return reject(error);
        }
        if(removal==='interface'&&result.ok===true)return resolve(result);
        if((removal==='backup'||removal==='provider'||removal==='change')&&result.ok===true&&result.result)return resolve(result.result);
        if(removal && result.ok===true && result.result?.revision)return resolve(result.result);
        if(result.ok!==true||!result.settings?.revision)throw new Error();
        resolve(result.settings);
      }catch{reject(failure({exit_code:code}));}
    });
    child.stdin.end(input);
  });
}

class Backend {
  constructor({ packageRoot, metadata, installHome, bootstrapHome, emit, setupHomes, environment = process.env, diagnostics }) {
    Object.assign(this, { packageRoot, metadata, installHome, bootstrapHome, emit, setupHomes, environment });
    this.diagnostics=diagnostics || new Diagnostics(bootstrapHome?path.join(bootstrapHome,'diagnostics'):null,metadata);
    this.child = null;
    this.active = false;
    this.prompt = null;
    this.links = new Set();
    this.abort = null;
    this.stopped = Promise.resolve();
  }
  event(data) {
    this.diagnostics.observe(data);
    if (data.type === 'prompt') this.prompt = data;
    if (data.type === 'link' && allowedLink(data.url)) this.links.add(data.url);
    if (data.type === 'overview') this.overview = data;
    this.emit(data);
  }
  async prepare(signal, { settingsOnly = false } = {}) {
    const progress = data => { if(!settingsOnly)this.event(data); };
    const target = process.platform === 'win32' ? 'windows' : 'macos';
    if (!['darwin', 'win32'].includes(process.platform)) throw new Error('This package requires macOS or Windows.');
    progress({ type: 'activity', active: true, message: 'Verifying the installer…' });
    await verifyPackage(this.packageRoot, this.metadata, target);
    progress({type:'diagnostic',operation:'package',status:'completed'});
    if (signal.aborted) throw new Error('Setup cancelled.');
    if(!settingsOnly) {
      await fs.mkdir(this.installHome, { recursive: true, mode: 0o700 });
      if (path.resolve(await fs.realpath(this.installHome)) !== path.resolve(this.installHome)) throw new Error('The installation directory must be direct.');
    }
    const env = cleanEnvironment(this.environment);
    const pythonRoot = path.join(this.bootstrapHome, 'python');
    // Resolving the parent prevents an inherited symlink from redirecting writes.
    await fs.mkdir(this.bootstrapHome, { recursive: true, mode: 0o700 });
    if (path.resolve(await fs.realpath(this.bootstrapHome)) !== path.resolve(this.bootstrapHome)) throw new Error('The private bootstrap directory must be direct.');
    Object.assign(env, { UV_CACHE_DIR: path.join(this.bootstrapHome, 'cache'), UV_PYTHON_INSTALL_DIR: pythonRoot, UV_NO_CONFIG: '1' });
    const uv = path.join(this.packageRoot, 'bin', target === 'windows' ? 'uv.exe' : 'uv');
    const options = { env, cwd: this.bootstrapHome };
    if (!(await command(uv, ['--version'], options, signal, 12000)).startsWith('uv 0.12.16 ')) throw new Error('Unsupported Python bootstrap version.');
    progress({ type: 'activity', active: true, message: 'Preparing private Python. The first download can take a few minutes…' });
    progress({type:'diagnostic',operation:'python',status:'started'});
    await command(uv, ['python', 'install', '3.12', '--managed-python', '--no-bin', '--no-registry', '--no-config', '--no-progress'], options, signal);
    const python = await command(uv, ['python', 'find', '3.12', '--managed-python', '--no-python-downloads', '--no-config'], options, signal, 30000);
    const relative = path.relative(pythonRoot, python);
    if (!relative || relative.startsWith('..') || path.isAbsolute(relative) || !(await fs.stat(python)).isFile()) throw new Error('Private Python is unavailable.');
    progress({type:'diagnostic',operation:'python',status:'completed'});
    progress({type:'diagnostic',operation:'libraries',status:'started'});
    const libraries = path.join(this.bootstrapHome, 'libraries');
    await fs.mkdir(libraries, { recursive: true, mode: 0o700 });
    if (path.resolve(await fs.realpath(libraries)) !== path.resolve(libraries)) throw new Error('The private libraries directory must be direct.');
    progress({ type: 'activity', active: true, message: 'Verifying the private setup libraries…' });
    await command(uv, ['pip', 'install', '--python', python, '--require-hashes', '--no-deps',
      '--only-binary', ':all:', '--target', libraries, '-r', path.join(this.packageRoot, 'payload', 'requirements-companion.lock')], options, signal);
    progress({type:'diagnostic',operation:'libraries',status:'completed'});
    const values = { PACKAGE_ROOT: this.packageRoot, INSTALL_HOME: this.installHome,
      BUNDLE_ROOT: path.join(this.packageRoot, 'payload'), UV_EXECUTABLE: uv,
      UV_SHA256: this.metadata.uv_sha256, TARGET_OS: target, OPENCLAW_API_PORT: '8000', OPENCLAW_GATEWAY_PORT: '18789',
      REVISION: /^[a-f0-9]{40}$/.test(this.metadata.revision || '') ? this.metadata.revision : '' };
    for (const [key, value] of Object.entries(values)) env['DAEMON_HARNESS_COMPANION_' + key] = value;
    Object.assign(env, { PYTHONUTF8: '1', PYTHONDONTWRITEBYTECODE: '1' });
    const script = "import sys,runpy;sys.path.insert(0,sys.argv.pop(1));sys.path.insert(1,sys.argv.pop(1));runpy.run_module('daemons_companion.desktop.protocol',run_name='__main__')";
    return { python, args: [...PYTHON_FLAGS, '-c', script, path.join(this.packageRoot, 'payload', 'src'), libraries], env, libraries };
  }
  hostDiagnostic(profile) {
    const id=require('node:crypto').createHash('sha256').update(profile).digest('hex');
    return new Diagnostics(this.bootstrapHome?path.join(this.bootstrapHome,'host-diagnostics',id):null,this.metadata);
  }
  async daemonSettings(request,removal=false) {
    if(this.active || this.settingsBusy)throw new Error('Finish or stop setup before managing another instance.');
    this.settingsBusy=true;
    let finished; this.settingsStopped=new Promise(resolve=>{finished=resolve;});
    const diagnostic=removal==='attachment'&&request.source_profile&&!['upgrade-status','upgrade-check','create-status'].includes(request.operation)?this.hostDiagnostic(request.source_profile):null;
    if(diagnostic){diagnostic.begin();diagnostic.record({phase:'installation',operation:request.operation,status:'started'});}
    try {
      if(!this.settingsRuntime)this.settingsRuntime=await this.prepare(new AbortController().signal,{settingsOnly:true});
      const runtime=this.settingsRuntime;
      const module=removal==='interface'?'runtime.host_interface':removal==='backup'?'backup.command':removal==='provider'?'desktop.provider_settings':removal==='change'?'hosts.change':removal==='attachment'?'hosts.attachment_command':removal==='discard'?'setup.discard':removal?'removal.removal':'desktop.daemon_settings';
      const script="import sys,runpy;sys.path.insert(0,sys.argv.pop(1));sys.path.insert(1,sys.argv.pop(1));runpy.run_module('daemons_companion."+module+"',run_name='__main__')";
      const transport=path.join(this.packageRoot,'payload','scripts','companion_attachment_transport.py');
      const args=[...PYTHON_FLAGS,'-c',removal==='attachment'?"import sys,runpy;sys.path.insert(0,sys.argv.pop(1));sys.path.insert(1,sys.argv.pop(1));runpy.run_path(sys.argv.pop(1),run_name='__main__')":script,path.join(this.packageRoot,'payload','src'),runtime.libraries];
      if(removal==='attachment')args.push(process.platform==='win32'?path.toNamespacedPath(transport):transport);
      const result=await settingsCommand(runtime.python,args,
        {env:runtime.env,cwd:this.bootstrapHome},request,removal,
        removal==='attachment'&&request.operation==='create-key'&&request.values?.kind==='codex_cli'?{
          child:child=>{this.authChild=child;this.authCancelled=false;},
          cancelled:()=>this.authCancelled===true,
          event:value=>{if(value.url)this.links.add(value.url);
            if(value.stage||value.catalog)diagnostic?.record({phase:'model',operation:value.catalog?'model-check':'sign-in',status:value.catalog?'completed':'started',auth_stage:value.stage,model_catalog:value.catalog});
            this.emit({type:'host-chatgpt',...value});}
        }:null);
      if(diagnostic){
        const failed=['rolled_back','rollback_failed','command_recovery'].includes(result.status);
        diagnostic.record({phase:'installation',operation:request.operation,status:failed?'failed':'completed',error_code:failed?'attachment.'+result.status:undefined});
        diagnostic.observe({type:'finished',result:failed?'failed':'completed'});
      }
      return result;
    } catch(error) {
      if(diagnostic){diagnostic.record({...error.attachmentDiagnostic,phase:'installation',operation:request.operation,status:'failed',error_code:error.message});diagnostic.observe({type:'finished',result:'failed'});}
      throw error;
    } finally {this.authChild=null;this.authCancelled=false;this.emit({type:"host-chatgpt-end"});this.settingsBusy=false;finished();}
  }
  async start(mode = 'setup', setupId = null, hostAddition = null, restoreArchive = null, move = false, hostChange = null, providerChange = null, daemonUpdate = null) {
    if (this.active || this.settingsBusy) throw new Error('Setup or daemon settings are already running.');
    if (!['setup', 'new', 'status', 'add-host', 'change-provider', 'update-daemon'].includes(mode)) throw new Error('Unsupported action.');
    if ((mode === 'update-daemon') !== Boolean(daemonUpdate && ['source_profile','base'].every(k=>typeof daemonUpdate[k]==='string'&&path.isAbsolute(daemonUpdate[k])) && typeof daemonUpdate.name==='string')) throw new Error('Unsupported action.');
    if ((mode === 'add-host') !== Boolean(hostAddition)) throw new Error('Unsupported action.');
    if ((mode === 'change-provider') !== Boolean(providerChange && ['profile','base'].every(k=>typeof providerChange[k]==='string'&&path.isAbsolute(providerChange[k])) && typeof providerChange.name==='string')) throw new Error('Unsupported action.');
    if (restoreArchive && (mode !== 'new' || !path.isAbsolute(restoreArchive))) throw new Error('Unsupported action.');
    if (hostChange && (mode !== 'new' || restoreArchive || !['profile','base'].every(k=>typeof hostChange[k]==='string'&&path.isAbsolute(hostChange[k])))) throw new Error('Unsupported action.');
    if (restoreArchive) {
      // Take the archive before returning: the caller removes its own folder as
      // soon as this call resolves, while the setup below runs asynchronously.
      this.active = true;
      try {
        const pending = path.join(this.bootstrapHome, 'restore-pending');
        await fs.mkdir(pending, {recursive: true, mode: 0o700});
        const owned = path.join(pending, crypto.randomUUID() + '.zip');
        await moveFile(restoreArchive, owned);
        restoreArchive = owned;
      } catch (error) { this.active = false; throw error; }
    }
    this.restoreArchive = restoreArchive; this.restoreMove = Boolean(restoreArchive && move); this.hostChange = hostChange;
    this.active = true;
    this.prompt = null;
    this.links.clear();
    this.abort = new AbortController();
    this.stopped = this._start(mode, setupId, hostAddition, providerChange, daemonUpdate);
    return { started: true };
  }
  async _start(mode, setupId = null, hostAddition = null, providerChange = null, daemonUpdate = null) {
    if(mode!=='status'){this.diagnosticHome=null;this.diagnostics=new Diagnostics(null,this.metadata);this.diagnostics.begin();}
    try {
      if(mode==='add-host'||mode==='change-provider'||mode==='update-daemon') {
        // The same guided session as a setup; its report stays with the daemon's host diagnostic.
        this.installHome=path.join(this.bootstrapHome,{'add-host':'host-addition','change-provider':'provider-change','update-daemon':'daemon-update'}[mode]);
        this.diagnostics=this.hostDiagnostic(mode==='add-host'?hostAddition.source_profile:mode==='update-daemon'?daemonUpdate.source_profile:providerChange.profile);
        this.diagnostics.begin();
      } else if(this.setupHomes) {
        this.installHome = await (mode==='new' ? this.setupHomes.create() : setupId ? this.setupHomes.select(setupId) : this.setupHomes.current());
        if(mode!=='new' && await fs.access(path.join(this.installHome,'setup-discard.json')).then(()=>true,()=>false))throw new Error('This installation cannot be resumed.');
        this.overview = null;
        if(mode==='new' && this.restoreArchive) {
          // Setup deletes it once the memory is restored; a resumed setup finds it here until then.
          await fs.mkdir(path.join(this.installHome,'restore'),{mode:0o700});
          await moveFile(this.restoreArchive,restoredArchive(this.installHome));
          if(this.restoreMove)await fs.writeFile(path.join(this.installHome,'restore','move'),'',{mode:0o600,flag:'wx'});
        }
        if(mode==='new' && this.hostChange) {
          // The setup reads the daemon to move from here, and records each step beside it.
          await fs.mkdir(path.dirname(hostChangeRequest(this.installHome)),{mode:0o700});
          await fs.writeFile(hostChangeRequest(this.installHome),JSON.stringify({schema:'daemon.host-change-request.v1',
            source_profile:this.hostChange.profile,base:this.hostChange.base})+'\n',{mode:0o600,flag:'wx'});
        }
      }
      if(!['status','add-host','change-provider','update-daemon'].includes(mode)) {
        this.diagnosticHome=this.installHome;
        this.diagnostics=new Diagnostics(this.installHome?path.join(this.installHome,'diagnostics'):null,this.metadata);
        this.diagnostics.begin();
      }
      const { python, args, env } = await this.prepare(this.abort.signal);
      env.DAEMONS_CONVERSATION_LANGUAGE = this.conversationLanguage === 'fr' ? 'fr' : 'en';
      if (['new','setup'].includes(mode) && this.installHome && await fs.access(restoredArchive(this.installHome)).then(()=>true,()=>false))
        env.DAEMONS_RESTORE_ARCHIVE = restoredArchive(this.installHome);
      // A move, confirmed by the user, also restores the upcoming reminders.
      if (env.DAEMONS_RESTORE_ARCHIVE && await fs.access(path.join(this.installHome,'restore','move')).then(()=>true,()=>false))
        env.DAEMONS_RESTORE_MOVE = '1';
      // A resumed host change continues from its record.
      if (['new','setup'].includes(mode) && this.installHome && await fs.access(hostChangeRequest(this.installHome)).then(()=>true,()=>false))
        env.DAEMONS_HOST_CHANGE = hostChangeRequest(this.installHome);
      if (mode === 'status') args.push('--status');
      if (mode === 'add-host') { args.push('--add-host'); env.DAEMONS_ADD_HOST = JSON.stringify(hostAddition); }
      if (mode === 'change-provider') { args.push('--change-provider'); env.DAEMONS_CHANGE_PROVIDER = JSON.stringify(providerChange); }
      if (mode === 'update-daemon') { args.push('--update-daemon'); env.DAEMONS_UPDATE_DAEMON = JSON.stringify(daemonUpdate); }
      if (this.abort.signal.aborted) throw new Error('Setup cancelled.');
      await new Promise((resolve, reject) => {
        const child = this.child = spawn(python, args, { env, cwd: this.installHome,
          shell: false, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
        const decoder = new StringDecoder('utf8');
        let buffer = '', finished = false;
        child.stdin.on('error', () => {});
        child.stderr.on('data', () => {});
        child.stdout.on('data', bytes => {
          buffer += decoder.write(bytes);
          let index;
          while ((index = buffer.indexOf('\n')) >= 0) {
            const line = buffer.slice(0, index); buffer = buffer.slice(index + 1);
            try {
              if (Buffer.byteLength(line) > MAX_FRAME) throw new Error();
              const frame = JSON.parse(line);
              if (frame.schema !== SCHEMA) throw new Error();
              if (frame.type === 'finished') finished = true;
              this.event(frame);
            } catch { this.event({type:'diagnostic',status:'failed',error_code:'invalid-frame'});this.cancel(); }
          }
          if (Buffer.byteLength(buffer) > MAX_FRAME) { buffer = ''; this.cancel(); }
        });
        child.on('error', reject);
        child.on('close', () => {
          if (!finished) this.event({ type: 'finished', result: 'error', error_code:'process-exit', message: 'Setup stopped unexpectedly. Its private recovery journal is retained. Reopen setup to check recovery.' });
          resolve();
        });
      });
    } catch (error) {
      this.event({ type: 'finished', result: this.abort.signal.aborted ? 'cancelled' : 'error',
        error_code:'UnexpectedError', message: this.abort.signal.aborted ? 'Setup cancelled. You can resume later.' : error.message });
    } finally {
      // A decrypted backup the setup never took is not kept.
      if (this.restoreArchive && path.basename(path.dirname(this.restoreArchive)) === 'restore-pending')
        await fs.rm(this.restoreArchive, {force: true}).catch(() => {});
      this.active = false; this.child = null; this.prompt = null;
      this.event({ type: 'idle' });
    }
  }
  answer(answer) {
    const prompt = this.prompt;
    if (!this.active || !this.child || !prompt || !answer ||
        Object.keys(answer).sort().join(',') !== 'action,id,value' || answer.id !== prompt.id ||
        !['submit', 'back', 'quit'].includes(answer.action)) throw new Error('Use the current setup question.');
    if (answer.action === 'submit') {
      if (prompt.kind === 'choice' ? !Number.isInteger(answer.value) || answer.value < 0 || answer.value >= prompt.options.length :
        typeof answer.value !== 'string' || !answer.value.trim() || answer.value.length > prompt.maximum || /[\x00-\x1f]/.test(answer.value)) throw new Error('Check your answer and try again.');
    } else if (answer.value !== null) throw new Error('Invalid navigation.');
    this.prompt = null;
    this.diagnostics.record({status:'started'});
    this.child.stdin.write(JSON.stringify({ schema: SCHEMA, type: 'answer', ...answer }) + '\n');
  }
  cancel() {
    this.prompt = null;
    if (this.child) this.child.stdin.write(JSON.stringify({ schema: SCHEMA, type: 'cancel' }) + '\n');
    else this.abort?.abort();
    this.event({ type: 'activity', active: true, message: 'Stopping safely. Please wait for cleanup…' });
  }
}

function restoredArchive(home) { return path.join(home,'restore','backup.zip'); }
function hostChangeRequest(home) { return path.join(home,'host-change','request.json'); }
async function moveFile(source,target) {
  try {await fs.rename(source,target);}
  catch(error) {
    if(error.code!=='EXDEV')throw error;
    await fs.copyFile(source,target,require('node:fs').constants.COPYFILE_EXCL);await fs.unlink(source);
  }
}

module.exports = { Backend, verifyPackage, cleanEnvironment, allowedLink, settingsCommand, PYTHON_FLAGS };
