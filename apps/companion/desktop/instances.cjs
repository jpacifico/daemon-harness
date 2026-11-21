'use strict';
// Main-process-only local inventory. No host executable is invoked for discovery.
const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const {execFile} = require('node:child_process');
const {promisify} = require('node:util');
const JSON5 = require('json5');
const yaml = require('js-yaml');
const plist = require('plist');
const execute = promisify(execFile);
const {INSTANCE_DIRECTORY} = require('./setup-homes.cjs');
const hostAssociations=require('./host-associations.cjs');
const hash = v => crypto.createHash('sha256').update(v).digest('hex');
const MAX = 1024 * 1024;
const clean = v => typeof v === 'string' && v.length <= 1024 && !/[\x00-\x1f]/.test(v) ? v : '';
const kinds = new Set(['hermes','openclaw']);
const envKeys = new Set(['HERMES_HOME','OPENCLAW_STATE_DIR','HERMES_CONFIG','DAEMON_CORE_ENV_FILE','DAEMON_CORE_MEMORY_ROOT','DAEMON_CORE_SRC','DAEMON_CORE_STATE_ROOT','DAEMON_CORE_HOST_REF','DAEMON_CORE_AFTER_TURN_SPOOL_DIR','DAEMON_CORE_CONTINUITY_RECEIPT_DIR']);
function allowedEnv(input = {}) { return Object.fromEntries(Object.entries(input).filter(([k,v]) => envKeys.has(k) && clean(v))); }
async function regular(file, limit = MAX) {
  const s = await fs.lstat(file);
  if (!s.isFile() || s.isSymbolicLink() || s.size > limit) throw new Error('Unreadable metadata.');
  const data = await fs.readFile(file, 'utf8');
  if (Buffer.byteLength(data) > limit) throw new Error('Oversized metadata.');
  return data;
}
async function directory(file) { try { const r = await fs.realpath(file); return (await fs.stat(r)).isDirectory() ? r : null; } catch { return null; } }
async function children(root) { try { return (await fs.readdir(root,{withFileTypes:true})).filter(x=>x.isDirectory()||x.isSymbolicLink()).slice(0,128).map(x=>path.join(root,x.name)); } catch { return []; } }
async function json(file) { return JSON5.parse(await regular(file)); }
function absolute(value, home) { if(!clean(value)) return ''; const v=value.startsWith('~/') ? path.join(home,value.slice(2)) : value; return path.isAbsolute(v) ? path.normalize(v) : ''; }
async function envFile(file, keys = envKeys) {
  if (!file) return {};
  try {
    const result = {};
    for (const line of (await regular(file)).split(/\r?\n/)) {
      const m = /^(?:export\s+)?([A-Z_]+)\s*=\s*(.*)$/.exec(line);
      if (!m || !keys.has(m[1])) continue;
      let v=m[2].trim();
      // The private runtime writes json.dumps values: on Windows each path
      // backslash is escaped. Only the escapes json.dumps emits for a path are
      // decoded, so a hand-written "C:\Users\..." keeps its backslashes.
      if (v.length>1 && v.startsWith('"') && v.endsWith('"')) v=v.slice(1,-1).replace(/\\(["\\]|u[0-9a-fA-F]{4})/g,(_,e)=>e.length===1?e:String.fromCharCode(parseInt(e.slice(1),16)));
      else if (v.length>1 && v.startsWith("'") && v.endsWith("'")) v=v.slice(1,-1);
      if(clean(v))result[m[1]]=v;
    }
    return result;
  } catch { return {}; }
}
function within(root, child) { const r=path.relative(root,child); return r==='' || (!r.startsWith('..') && !path.isAbsolute(r)); }
function argument(args, flag) { const i=args.indexOf(flag); return i>=0 && typeof args[i+1]==='string' ? args[i+1] : ''; }
const SERVICE = /^(?:ai\.(?:hermes|openclaw)\.[A-Za-z0-9_.-]+|com\.daemonharness\.[A-Za-z0-9_.-]+|OpenClaw Gateway(?: \([A-Za-z0-9_-]+\))?|Hermes Gateway)$/;
const gatewayFor = (label,host) => label.toLowerCase().includes(host) && label.toLowerCase().includes('gateway') && !label.includes('openclaw-api');

class NativeServices {
  constructor({home,platform=process.platform,run=execute}) { Object.assign(this,{home,platform,run}); }
  async command(exe,args) { return this.run(exe,args,{timeout:15000,maxBuffer:2*MAX,windowsHide:true,encoding:'utf8',env:Object.fromEntries(Object.entries(process.env).filter(([key])=>key.toLowerCase()!=='psmodulepath'))}); }
  async list() {
    if(this.platform==='darwin') {
      let states = new Map(), statusAvailable=true;
      try { for(const line of (await this.command('/bin/launchctl',['list'])).stdout.split('\n')) {
        const [pid,exit,label]=line.trim().split(/\s+/); if(SERVICE.test(label||'')) states.set(label,{running:/^[1-9][0-9]*$/.test(pid),exit});
      } } catch { statusAvailable=false; }
      const root=path.join(this.home,'Library/LaunchAgents');
      let names; try {names=await fs.readdir(root);}catch{return {items:[],available:statusAvailable};}
      const items=[];
      for(const name of names.filter(n=>n.endsWith('.plist')&&SERVICE.test(n.slice(0,-6))).slice(0,256)) {
        try {
          const file=path.join(root,name), raw=await regular(file), d=plist.parse(raw);
          if(d.Label!==name.slice(0,-6)||!Array.isArray(d.ProgramArguments)||!d.ProgramArguments.every(x=>typeof x==='string'))continue;
          items.push({label:d.Label,file,digest:hash(raw),args:d.ProgramArguments,cwd:clean(d.WorkingDirectory),env:allowedEnv(d.EnvironmentVariables),
            state:statusAvailable?(states.get(d.Label)?.running?'running':'stopped'):'unknown'});
        }catch { /* Skip a malformed definition; no raw exception leaves this layer. */ }
      }
      return {items,available:statusAvailable};
    }
    if(this.platform==='win32') {
      // Read only current-user known task definitions; never dump them to the renderer.
      const script = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);$ErrorActionPreference='Stop';$s=New-Object -ComObject Schedule.Service;$s.Connect();$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value;$out=@();foreach($folderName in @('\\','\\DaemonHarness')){try{$folder=$s.GetFolder($folderName)}catch{if($_.Exception.GetBaseException().HResult -in @(-2147024894,-2147024893)){continue};throw};foreach($t in $folder.GetTasks(0)){if($t.Name -notmatch '^(ai\\.(hermes|openclaw)\\.[A-Za-z0-9_.-]+|com\\.daemonharness\\.[A-Za-z0-9_.-]+|OpenClaw Gateway( \\([A-Za-z0-9_-]+\\))?|Hermes Gateway)$'){continue};$d=$t.Definition;$owner=$d.Principal.UserId;if($owner -ne $sid){try{$owner=([System.Security.Principal.NTAccount]::new($owner)).Translate([System.Security.Principal.SecurityIdentifier]).Value}catch{continue}};if($owner -ne $sid -or $d.Principal.RunLevel -ne 0){continue};$actions=$d.Actions;if($actions.Count -ne 1){continue};$a=$actions.Item(1);if($a.Type -ne 0){continue};$out+=@{label=$t.Name;folder=$folderName;xml=$t.Xml;executable=$a.Path;arguments=$a.Arguments;cwd=$a.WorkingDirectory;state=[int]$t.State}}};ConvertTo-Json -InputObject @($out) -Depth 4 -Compress";
      try {
        const v=JSON.parse((await this.command('powershell.exe',['-NoLogo','-NoProfile','-NonInteractive','-Command',script])).stdout.replace(/^\uFEFF/,''));
        if(!Array.isArray(v)||v.length>256)throw new Error();
        return {available:true,items:v.filter(x=>SERVICE.test(x.label)&&['\\','\\DaemonHarness'].includes(x.folder)&&typeof x.xml==='string').map(x=>({label:x.label,folder:x.folder,digest:hash(x.xml),args:[x.executable,x.arguments],cwd:clean(x.cwd),env:{},state:x.state===4?'running':x.state===3?'stopped':'unknown'}))};
      }catch{return {items:[],available:false};}
    }
    return {items:[],available:false};
  }
  async control(service,action) {
    if(!SERVICE.test(service.label)||!['start','stop','restart'].includes(action))throw new Error('Unsupported gateway action.');
    if(this.platform==='win32'&&!['\\','\\DaemonHarness'].includes(service.folder))throw new Error('Unsupported service folder.');
    if(action==='start'&&service.state==='running')return;
    if(this.platform==='darwin') {
      const target=`gui/${process.getuid()}/${service.label}`;
      if(action==='stop') {
        let loaded=true;try{await this.command('/bin/launchctl',['print',target]);}catch{loaded=false;}
        if(loaded)await this.command('/bin/launchctl',['bootout',target]);
        return;
      }
      let loaded=true;try {await this.command('/bin/launchctl',['print',target]);}catch{loaded=false;}
      if(!loaded)await this.command('/bin/launchctl',['bootstrap',`gui/${process.getuid()}`,service.file]);
      await this.command('/bin/launchctl',['kickstart',...(action==='restart'?['-k']:[]),target]);
    } else if(this.platform==='win32') {
      // Name and action are validated above; XML is never executed by this code.
      const label=service.label;
      if(!['\\','\\DaemonHarness'].includes(service.folder))throw new Error('Unsupported service folder.');
      const script="$ErrorActionPreference='Stop';$s=New-Object -ComObject Schedule.Service;$s.Connect();$t=$s.GetFolder('"+service.folder+"').GetTask('"+label+"');"+
        (action!=='start'?"if($t.State -eq 4){$t.Stop(0);for($i=0;$i -lt 50 -and $t.State -eq 4;$i++){Start-Sleep -Milliseconds 100};if($t.State -eq 4){throw 'The service did not stop'}};":"")+(action!=='stop'?"$null=$t.Run($null);":"");
      await this.command('powershell.exe',['-NoLogo','-NoProfile','-NonInteractive','-Command',script]);
    } else throw new Error('Unsupported platform.');
  }
}

// An OpenClaw daemon created by the app has its own private OpenClaw in its setup folder.
// One added to a Hermes daemon (Companion/private) is removed with that daemon only.
function ownOpenClaw(base,profile) {
  const parts=path.relative(path.join(base,'Companion'),profile).split(path.sep);
  return parts.length===6&&/^instance-[A-Za-z0-9]{6}$/.test(parts[0])&&parts[1]==='hosts'&&parts[2]==='openclaw'&&parts[3]==='installations'&&/^[a-f0-9]{32}$/.test(parts[4])&&parts[5]==='profile';
}
class Instances {
  constructor({home,appData,base,platform=process.platform,environment=process.env,services}) {
    Object.assign(this,{home,appData,base,platform,environment});
    this.services=services||new NativeServices({home,platform}); this.records=[]; this.rows=new Map(); this.busy=false; this.daemonReviews=new Map();
  }
  async load() {
    try {
      const p=JSON.parse(await regular(path.join(this.appData,'instances.json'),65536));
      if(p.schema!==1||!Array.isArray(p.profiles)||p.profiles.length>128)throw new Error();
      this.records=p.profiles.filter(x=>kinds.has(x.host)&&path.isAbsolute(clean(x.profile))&&
        (x.grant===null||(x.grant && SERVICE.test(x.grant.label)&&/^[a-f0-9]{64}$/.test(x.grant.digest))));
    }catch(e) {if(e.code!=='ENOENT')this.registryWarning=true;this.records=[];}
  }
  async save(records=this.records) {
    await fs.mkdir(this.appData,{recursive:true,mode:0o700});
    if(await fs.realpath(this.appData)!==path.resolve(this.appData))throw new Error('The app settings directory must be direct.');
    const target=path.join(this.appData,'instances.json'),tmp=target+'.'+crypto.randomUUID();
    await fs.writeFile(tmp,JSON.stringify({schema:1,profiles:records})+'\n',{mode:0o600,flag:'wx'});
    try {await fs.rename(tmp,target);}finally{await fs.rm(tmp,{force:true});}
  }
  async profile(host,folder,services) {
    const profile=await directory(folder); if(!profile)return null;
    const config=path.join(profile,host==='hermes'?'config.yaml':'openclaw.json');
    let data, problem='';
    try {data=host==='hermes'?yaml.load(await regular(config),{schema:yaml.JSON_SCHEMA}):await json(config);if(!data||typeof data!=='object'||Array.isArray(data))throw new Error();}
    catch(e){if(e.code==='ENOENT')return null;data={};problem='configuration-unreadable';}
    const env=await envFile(path.join(profile,'.env'));
    const extra=await envFile(absolute(env.DAEMON_CORE_ENV_FILE,this.home));
    const selected= {...extra,...env};
    let daemon=null, binding=null;
    if(host==='hermes') {
      const account=await envFile(path.join(profile,'.env'),new Set(['TELEGRAM_ALLOWED_USERS']));
      const id=account.TELEGRAM_ALLOWED_USERS?.trim();
      if(/^[0-9]{1,20}$/.test(id||''))binding={user_id:id,platform:'telegram'};
    }
    if(host==='hermes') {
      const plugin=await directory(path.join(profile,'plugins/daemon_core'));
      if(plugin)daemon={state:'detected',source:await directory(absolute(selected.DAEMON_CORE_SRC,this.home)),memory:await directory(absolute(selected.DAEMON_CORE_MEMORY_ROOT,this.home))};
    } else {
      const plugins=data.plugins||{},entry=plugins.entries?.['daemon-harness'];
      if(entry || plugins.slots?.contextEngine==='daemon-harness') {
        daemon={state:entry?.enabled!==false&&plugins.slots?.contextEngine==='daemon-harness'?'configured':'detected',source:null,memory:null};
        const cfg=entry?.config||{};
        if(clean(cfg.userId) && cfg.userId.length<=128)binding={user_id:cfg.userId,platform:'openclaw'};
        const port=(()=>{try{const u=new URL(cfg.daemonUrl);return ['127.0.0.1','localhost','[::1]'].includes(u.hostname)?u.port||'80':'';}catch{return '';}})();
        const api=services.filter(s=>s.label.includes('openclaw-api')&&argument(s.args,'--port')===port&&port);
        if(Object.hasOwn(selected,'DAEMON_CORE_HOST_REF')) {
          try {Object.assign(daemon,await hostAssociations.resolve(profile,selected,cfg));}
          catch {problem='association-unreadable';}
        }
        else if(api.length===1){const e={...await envFile(absolute(api[0].env.DAEMON_CORE_ENV_FILE,this.home)),...api[0].env};Object.assign(selected,e);daemon.memory=await directory(absolute(e.DAEMON_CORE_MEMORY_ROOT,this.home));daemon.source=await directory(absolute(e.DAEMON_CORE_SRC,this.home));}
      }
    }
    if(daemon)daemon.runtime=daemon.memory;
    if(daemon?.memory && path.basename(daemon.memory)==='.daemon')daemon.memory=path.dirname(daemon.memory);
    if(daemon){daemon.id=daemon.memory?hash('memory:'+daemon.memory).slice(0,20):null;daemon.name='';if(daemon.memory){try{const d=await json(path.join(daemon.runtime,'core/daemon_identity.json'));daemon.name=clean(d.name);}catch{/* Legacy/absent identity: do not invent it. */}}}
    const attempt=path.dirname(profile);
    if(within(this.base,attempt)) {
      const setup=path.resolve(attempt,'../../../..');
      if(path.dirname(setup)===path.join(this.base,'Companion'))try {
        const discarded=await json(path.join(setup,'setup-discard.json'));
        if(discarded.schema==='daemon.companion-setup-discard.v1')return null;
      }catch{/* Normal installations have no discard record. */}
    }
    let removal=null;
    try {const m=await json(path.join(attempt,'companion-removal.json'));
      if(m.schema==='daemon.companion-removal.v1'&&m.profile===profile){
        if(m.status==='done'&&m.action==='uninstall')return null;
        removal={action:m.action,status:m.status};
      }
    }catch{/* No removal receipt: regular discovery. */}
    const matches=services.filter(s=> {
      if(!gatewayFor(s.label,host))return false;
      const p=absolute(s.env[host==='hermes'?'HERMES_HOME':'OPENCLAW_STATE_DIR'],this.home);
      if(p)return path.resolve(p)===profile;
      const explicit=argument(s.args,host==='hermes'?'--hermes-home':'--state-dir');
      if(explicit)return path.resolve(explicit)===profile;
      if(s.cwd===profile)return true;
      return path.basename(profile)==='profile'&&s.cwd===attempt&&s.label.includes(path.basename(attempt));
    });
    const service=matches.length===1?matches[0]:null;
    const record=this.records.find(x=>x.host===host&&x.profile===profile);
    const managed=Boolean(service&&record?.grant?.label===service.label&&record.grant.digest===service.digest);
    const id=hash(host+':'+profile).slice(0,24);
    const row={id,host,profile,daemon,binding,service,managed,problem,removal,serviceProblem:matches.length>1?'ambiguous':!service?'unavailable':record?.grant&&!managed?'changed':'',
      state:service?.state||'unknown',connected:Boolean(record?.grant),manual:Boolean(record)};
    const sourceRoot=daemon?.source?path.dirname(daemon.source):null;
    const healthEnv={...selected,...await envFile(absolute(service?.env?.DAEMON_CORE_ENV_FILE,this.home)),...service?.env};
    const stateRoot=absolute(healthEnv.DAEMON_CORE_STATE_ROOT,this.home);
    row.runtimeState=stateRoot||null;
    const managedReceipts=stateRoot?path.join(stateRoot,'receipts'):null;
    row.healthPaths={spool:absolute(healthEnv.DAEMON_CORE_AFTER_TURN_SPOOL_DIR,this.home)||(sourceRoot?path.join(sourceRoot,'exports/after_turn_spool'):null),
      receipts:absolute(healthEnv.DAEMON_CORE_CONTINUITY_RECEIPT_DIR,this.home)||(managedReceipts&&await directory(managedReceipts)?managedReceipts:sourceRoot?path.join(sourceRoot,'exports/continuity_receipts'):null)};
    this.rows.set(id,row); return row;
  }
  async refresh() {
    const started=performance.now(); const snapshot=await this.services.list(); const candidates=[];
    const add=(host,profile)=>{if(kinds.has(host)&&profile)candidates.push({host,profile});};
    add('hermes',path.join(this.home,'.hermes'));add('openclaw',path.join(this.home,'.openclaw'));
    for(const p of await children(path.join(this.home,'.hermes/profiles')))add('hermes',p);
    for(const p of await children(this.home))if(path.basename(p).startsWith('.openclaw-'))add('openclaw',p);
    add('hermes',absolute(this.environment.HERMES_HOME,this.home));add('openclaw',absolute(this.environment.OPENCLAW_STATE_DIR,this.home));
    const companionRoots=(await children(path.join(this.base,'Companion'))).filter(p=>INSTANCE_DIRECTORY.test(path.basename(p)));
    for(const base of [this.base,path.join(this.base,'Companion/private'),...companionRoots])for(const host of kinds)
      for(const attempt of await children(path.join(base,'hosts',host,'installations')))add(host,path.join(attempt,'profile'));
    for(const s of snapshot.items){
      add('hermes',absolute(s.env.HERMES_HOME,this.home));add('openclaw',absolute(s.env.OPENCLAW_STATE_DIR,this.home));
      for(const host of kinds)if(gatewayFor(s.label,host)){add(host,s.cwd);add(host,path.join(s.cwd||this.home,'profile'));}
    }
    for(const r of this.records)add(r.host,r.profile);
    this.rows.clear();
    for(const x of candidates.slice(0,512))await this.profile(x.host,x.profile,snapshot.items);
    const rows=[...this.rows.values()];
    const missing=this.records.filter(x=>!rows.some(r=>r.profile===x.profile&&r.host===x.host)).length;
    return {instances:rows.map(r=>({id:r.id,host:r.host,profile:r.profile,state:r.state,managed:r.managed,connected:r.connected,registered:r.manual,problem:r.problem,
      removal:r.removal,removalSupported:['darwin','win32'].includes(this.platform)&&within(this.base,r.profile)&&(r.host==='hermes'||ownOpenClaw(this.base,r.profile)),
      serviceProblem:r.serviceProblem,canConnect:Boolean(r.service)&&!r.problem,
      daemon:r.daemon?{...r.daemon,sharedWith:rows.filter(x=>x.id!==r.id&&r.daemon.id&&x.daemon?.id===r.daemon.id).map(x=>x.id)}:null})),
      servicesAvailable:snapshot.available,registryWarning:Boolean(this.registryWarning),missing,elapsedMs:Math.round(performance.now()-started)};
  }
  async add(folder) {
    if(this.busy)throw new Error('An instance action is already running.');
    this.busy=true;try {
    const resolved=await directory(folder);if(!resolved)throw new Error('Choose an existing host profile folder.');
    const snapshot=await this.services.list();let found=false;const next=this.records.map(x=>({...x}));
    for(const host of kinds){const r=await this.profile(host,resolved,snapshot.items);if(r){found=true;if(!next.some(x=>x.host===host&&x.profile===r.profile))next.push({host,profile:r.profile,grant:null});}}
    if(!found)throw new Error('No Hermes or OpenClaw configuration was found in that folder.');
    if(next.length>128)throw new Error('Too many registered profiles.');
    await this.save(next);this.records=next;return await this.refresh();
    }finally{this.busy=false;}
  }
  async change(id,action,confirm) {
    if(this.busy)throw new Error('An instance action is already running.');
    if(!['connect','forget','start','stop','restart'].includes(action))throw new Error('Unsupported instance action.');
    this.busy=true;
    try {
      await this.refresh();const r=this.rows.get(id);if(!r)throw new Error('Refresh the instance list first.');
      if(action!=='forget' && (!r.service||r.problem))throw new Error('This gateway service cannot be identified safely.');
      if(!['connect','forget'].includes(action)&&(!r.managed||r.state==='unknown'))throw new Error('Connect this instance before controlling its gateway.');
      const fingerprint=r.service?.digest;
      if(!await confirm({action,host:r.host,profile:r.profile,shared:Boolean(r.daemon?.memory&&[...this.rows.values()].some(x=>x.id!==id&&x.daemon?.id===r.daemon?.id))}))return this.refresh();
      await this.refresh();const fresh=this.rows.get(id);
      if(!fresh || fresh.service?.digest!==fingerprint)throw new Error('The service changed. Refresh and confirm again.');
      if(['connect','forget'].includes(action)){
        const next=this.records.filter(x=>x.host!==r.host||x.profile!==r.profile);
        if(action==='connect')next.push({host:r.host,profile:r.profile,grant:{label:r.service.label,digest:fingerprint}});
        await this.save(next);this.records=next;
      } else {
        try {await this.services.control(fresh.service,action);} catch {throw new Error('The host service action failed. Refresh its status before retrying.');}
        const desired=action==='stop'?'stopped':'running';
        for(let n=0;n<10;n++){const observation=await this.refresh();if(observation.instances.find(x=>x.id===id)?.state===desired)return observation;await new Promise(r=>setTimeout(r,250));}
        throw new Error('The host service did not reach the requested state. Refresh its status.');
      }
      return this.refresh();
    }finally{this.busy=false;}
  }
  // After a model or key change: the app restarts the gateway of a daemon it created,
  // so the host reads the new settings. A stopped daemon stays stopped.
  async restartGateway(id) {
    if(this.busy)throw new Error('provider.busy');
    this.busy=true;
    try {
      await this.refresh();const r=this.rows.get(id);
      if(!r?.service||r.problem||r.serviceProblem||!(r.managed||within(this.base,r.profile)))throw new Error('provider.restart-failed');
      if(r.state!=='running')return false;
      await this.services.control(r.service,'restart');
      for(let n=0;n<40;n++){const observation=await this.refresh();if(observation.instances.find(x=>x.id===id)?.state==='running')return true;await new Promise(r=>setTimeout(r,250));}
      throw new Error('provider.restart-failed');
    }finally{this.busy=false;}
  }
  async health(id,evidence={}) {
    if(this.busy)throw new Error('An instance action is already running.');
    this.busy=true;
    try {await this.refresh();const row=this.rows.get(id);if(!row)throw new Error('Refresh the instance list first.');
      return await require('./health.cjs').collect(row,evidence);
    } finally {this.busy=false;}
  }
  async daemonContext(id) {
    await this.refresh();
    const row=this.rows.get(id);
    if(!row || row.problem || !row.daemon?.runtime)throw new Error('The daemon memory could not be identified safely.');
    const related=[...this.rows.values()].filter(r=>r.daemon?.id===row.daemon.id);
    const shared=related.filter(r=>r.id!==id).map(r=>({host:r.host,profile:r.profile})).sort((a,b)=>a.profile.localeCompare(b.profile));
    const accounts=related.filter(r=>r.binding).map(r=>({...r.binding,host:r.host})).sort((a,b)=>JSON.stringify(a).localeCompare(JSON.stringify(b)));
    const request={root:row.daemon.runtime,source:row.daemon.source,binding:row.binding};
    const signature=hash(JSON.stringify({profile:row.profile,host:row.host,request,shared,accounts}));
    return {row,request,shared,accounts,signature};
  }
  reviewDaemon(context,settings) {
    const token=crypto.randomUUID(),id=context.row.id;
    if(this.daemonReviews.size>=128)this.daemonReviews.clear();
    this.daemonReviews.set(id,{token,signature:context.signature,revision:settings.revision});
    // Discovery is not authorization to associate two accounts with one person.
    const accounts=new Map(),key=a=>JSON.stringify([a.platform,a.user_id]);
    for(const a of context.accounts) {
      const k=key(a),item=accounts.get(k)||{platform:a.platform,user_id:a.user_id,hosts:[],addressing:false};
      if(!item.hosts.includes(a.host))item.hosts.push(a.host);
      accounts.set(k,item);
    }
    if(settings.userScope) {
      const k=key(settings.userScope),item=accounts.get(k)||{...settings.userScope,hosts:[]};
      accounts.set(k,{...item,addressing:true});
    }
    return {id,token,host:context.row.host,profile:context.row.profile,memory:context.row.daemon.memory,
      shared:context.shared,...settings,accounts:[...accounts.values()]};
  }
  async inspectDaemon(id,run) {
    if(this.busy)throw new Error('An instance action is already running.');
    this.busy=true;
    try {const context=await this.daemonContext(id);
      const settings=await run({action:'inspect',...context.request});
      return this.reviewDaemon(context,settings);
    } finally {this.busy=false;}
  }
  async saveDaemon(id,token,values,run,confirm) {
    if(this.busy)throw new Error('An instance action is already running.');
    if(!values || Object.keys(values).sort().join(',')!=='gender,name,preferred_name,pronouns' ||
      Object.values(values).some(v=>typeof v!=='string'||[...v].length>40||/[\x00-\x1f]/.test(v)))throw new Error('Check the names and pronouns. Each field accepts up to 40 characters.');
    this.busy=true;
    try {
      const review=this.daemonReviews.get(id);
      if(!review || token!==review.token)throw new Error('These settings changed. Reopen the daemon settings before saving.');
      let context=await this.daemonContext(id);
      if(context.signature!==review.signature)throw new Error('These settings changed. Reopen the daemon settings before saving.');
      if(context.shared.length && !await confirm({...context,values}))return {cancelled:true};
      context=await this.daemonContext(id);
      if(context.signature!==review.signature)throw new Error('These settings changed. Reopen the daemon settings before saving.');
      const settings=await run({action:'save',...context.request,revision:review.revision,values});
      return this.reviewDaemon(context,settings);
    } finally {this.busy=false;}
  }
  // OpenClaw profiles created by Add a host for this daemon (their setup names it as source).
  async addedHosts(profile) {
    const root=path.join(this.base,'Companion/private/host-setups'),found=new Set();
    let names;try{names=await fs.readdir(root);}catch(error){if(error.code==='ENOENT')return found;throw error;}
    for(const name of names.filter(n=>/^[a-f0-9]{32}\.json$/.test(n)).slice(0,128)){
      const file=path.join(root,name),info=await fs.lstat(file);
      if(!info.isFile()||info.size>65536)continue;
      try{const data=JSON.parse(await fs.readFile(file,'utf8'));
        const expected=path.join(this.base,'Companion/private/hosts/openclaw/installations',name.slice(0,-5),'profile');
        if(data.source===profile&&data.profile===expected)found.add(expected);}catch{}
    }
    return found;
  }
  async removalContext(id) {
    const inventory=await this.refresh(),row=this.rows.get(id);
    if(!['darwin','win32'].includes(this.platform))throw new Error('removal.platform');
    if(!row||row.problem||!within(this.base,row.profile)||(row.host!=='hermes'&&!ownOpenClaw(this.base,row.profile)))throw new Error('removal.ownership');
    if(!inventory.servicesAvailable){const error=new Error('removal.services');error.removalDiagnostic={stage:'inventory'};throw error;}
    // Hosts added to this daemon are stopped by the removal itself; anything else
    // that uses its files blocks it. The removal's own task scan is the final check.
    const {samePath,key}=hostAssociations;
    const inside=(parent,child)=>{const a=key(parent),b=key(child);return Boolean(a&&b)&&(a===b||b.startsWith(a+path.sep));};
    const shared=stage=>{const error=new Error('removal.shared');error.removalDiagnostic={stage};return error;};
    const added=await this.addedHosts(row.profile),attempt=path.dirname(row.profile);
    const others=inventory.instances.filter(r=>r.id!==id&&![...added].some(p=>samePath(p,r.profile)));
    const sameMemory=r=>Boolean(row.daemon?.memory&&samePath(r.daemon?.memory,row.daemon.memory));
    if(others.some(r=>(r.problem||r.serviceProblem==='ambiguous')&&(inside(attempt,r.profile)||sameMemory(r))))throw shared('related_problem');
    if(others.some(sameMemory))throw shared('shared_memory');
    // Registered dependents block; an unreadable registry is left to the removal's own task scan.
    try {await hostAssociations.assertNoDependents(row.runtimeState,row.profile,added);}
    catch(error) {if(error.dependent)throw shared('host_registration');}
    // Another host may use this installation's runtime without sharing its vault.
    if(others.some(r=>inside(attempt,r.profile)||[r.daemon?.memory,r.daemon?.source].some(p=>p&&inside(attempt,p))))throw shared('nested_installation');
    return {row,signature:hash(JSON.stringify(inventory.instances.map(r=>({id:r.id,profile:r.profile,daemon:r.daemon,problem:r.problem}))))};
  }
  async reviewRemoval(id,action,run) {
    if(this.busy)throw new Error('An instance action is already running.');
    if(!['detach','uninstall'].includes(action))throw new Error('removal.request');
    this.busy=true;
    try {
      const context=await this.removalContext(id);
      const plan=await run({operation:'inspect',action,profile:context.row.profile,base:this.base});
      const token=crypto.randomUUID();
      const installationName=`${context.row.host==='hermes'?'Hermes':'OpenClaw'} - ${plan.name}`;
      const display={installationName,confirmationText:action==='detach'?plan.name:installationName,daemonAttached:Boolean(context.row.daemon),host:context.row.host};
      this.removalReview={id,token,signature:context.signature,plan,display};
      return {...plan,...display,id,token};
    }finally{this.busy=false;}
  }
  async applyRemoval(id,token,name,run) {
    if(this.busy)throw new Error('An instance action is already running.');
    const review=this.removalReview;
    if(!review||review.id!==id||review.token!==token||name!==review.display.confirmationText)throw new Error('removal.changed');
    this.busy=true;
    try {
      const context=await this.removalContext(id);
      if(context.signature!==review.signature)throw new Error('removal.changed');
      this.removalReview=null;
      const result=await run({operation:'apply',action:review.plan.action,profile:context.row.profile,base:this.base,revision:review.plan.revision,name:review.plan.name});
      this.removalResult={id,token,...result,...review.display};
      if(result.done){const records=this.records.filter(r=>r.profile!==context.row.profile||r.host!==context.row.host);await this.save(records);this.records=records;}
      return {...result,...review.display,id,token};
    }finally{this.busy=false;}
  }
  // After a finished uninstall, the vault it reviewed may go to the trash once
  // nothing listed here and no scheduled job still uses it.
  async memoryRemoval(id,token,run) {
    const record=this.removalResult;
    if(!record||record.id!==id||record.token!==token||!record.done||record.action!=='uninstall'||!record.memoryDeletable)throw new Error('removal.changed');
    const {key}=hostAssociations;
    const inside=(parent,child)=>{const a=key(parent),b=key(child);return Boolean(a&&b)&&(a===b||b.startsWith(a+path.sep));};
    const inventory=await this.refresh();
    if(inventory.instances.some(r=>[r.profile,r.daemon?.memory,r.daemon?.source].some(p=>p&&(inside(record.memory,p)||inside(p,record.memory)))))throw new Error('removal.shared');
    const checked=await run({operation:'memory-check',action:'uninstall',profile:record.profile,base:this.base,revision:record.revision});
    if(checked.memory!==record.memory||await fs.realpath(record.memory)!==record.memory)throw new Error('removal.unsafe');
    this.removalResult=null;
    return record.memory;
  }
  async removalTarget(id,token,kind) {
    const record=this.removalReview?.token===token?{...this.removalReview.plan,id:this.removalReview.id}:this.removalResult?.token===token?this.removalResult:null;
    if(!record||record.id!==id)throw new Error('removal.changed');
    const target=kind==='memory'?record.memory:kind==='profile'?record.profile:null;
    if(!target||await fs.realpath(target)!==target)throw new Error('removal.unsafe');
    return target;
  }
  async target(id,kind) {
    await this.refresh();const r=this.rows.get(id);const p=kind==='profile'?r?.profile:kind==='memory'?r?.daemon?.memory:null;
    if(!p||!await directory(p))throw new Error('This folder is unavailable.');
    return p;
  }
}
module.exports={Instances,NativeServices,allowedEnv};
