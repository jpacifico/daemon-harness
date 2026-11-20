'use strict';
// Only the main process chooses app-owned installation roots. No renderer path.
const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const {isDeepStrictEqual} = require('node:util');
async function metadata(file,maximum=8192) {
  let info;
  try { info = await fs.lstat(file); }
  catch(error) { if(error.code === 'ENOENT')return null; throw error; }
  if(!info.isFile() || info.isSymbolicLink() || info.size > maximum || await fs.realpath(file) !== file)
    throw new Error('Unreadable installation receipt.');
  const raw = await fs.readFile(file,'utf8');
  if(Buffer.byteLength(raw) > maximum)throw new Error('Oversized installation receipt.');
  const value = JSON.parse(raw);
  if(!value || typeof value !== 'object' || Array.isArray(value))throw new Error('Invalid installation receipt.');
  return value;
}
const INSTANCE_DIRECTORY = /^instance-[A-Za-z0-9]{6}$/;
class SetupHomes {
  constructor(root) { this.root = path.resolve(root); this.file = path.join(this.root, 'current-setup.json'); }
  async direct(directory, create = false) {
    if(create)await fs.mkdir(directory,{recursive:true,mode:0o700});
    if(path.resolve(await fs.realpath(directory))!==directory || !(await fs.lstat(directory)).isDirectory())
      throw new Error('The installation directory must be direct.');
    return directory;
  }
  async current() {
    let info;
    try { info = await fs.lstat(this.file); }
    catch(error) { if(error.code==='ENOENT')return path.join(this.root,'private'); throw error; }
    await this.direct(this.root);
    if(!info.isFile() || info.isSymbolicLink() || info.size>1024)throw new Error('The saved setup could not be read. Start a separate new installation or restore its selection file.');
    let data;
    try {data=JSON.parse(await fs.readFile(this.file,'utf8'));}catch{throw new Error('The saved setup could not be read. Start a separate new installation or restore its selection file.');}
    if(!data || data.schema!==1 || !(INSTANCE_DIRECTORY.test(data.directory) || data.directory==='private') || Object.keys(data).sort().join(',')!=='directory,schema')
      throw new Error('The saved setup could not be read. Start a separate new installation or restore its selection file.');
    return this.direct(path.join(this.root,data.directory));
  }
  async summary(directory = null) {
    // Presentation hint only. Reading this must never create or resume a setup.
    try {
      directory = directory || await this.current();
      let info;
      try { info = await fs.lstat(directory); }
      catch(error) { if(error.code === 'ENOENT')return 'none'; throw error; }
      if(!info.isDirectory() || info.isSymbolicLink())return 'unreadable';
      await this.direct(directory);
      if(path.basename(directory) === 'private' && !(await fs.readdir(directory)).length)return 'none';
      const discard=await metadata(path.join(directory,'setup-discard.json'));
      if(discard?.schema==='daemon.companion-setup-discard.v1')return discard.status==='done'?'none':'discarding';
      // A daemon removed by the app, created on Hermes Agent or directly on
      // OpenClaw, leaves only its retained files: no setup to show.
      for(const [name,schema] of [['hermes','daemon.companion-host-install.v1'],['openclaw','daemon.openclaw-first.v1']]) {
        const host=await metadata(path.join(directory,'hosts',name,'current.json'));
        if(host?.schema!==schema || !/^[0-9a-f]{32}$/.test(host.installation_id||''))continue;
        const attempt=path.join(directory,'hosts',name,'installations',host.installation_id);
        const removal=await metadata(path.join(attempt,'companion-removal.json'),2*1024*1024);
        if(removal?.schema==='daemon.companion-removal.v1' && removal.profile===path.join(attempt,'profile') && removal.status!=='rolled_back')return removal.status==='done'?'none':'unreadable';
      }
      // A host change whose new host awaits the user's check resumes like an unfinished setup.
      const change=await metadata(path.join(directory,'host-change/record.json'),256*1024);
      if(change?.schema==='daemon.host-change.v1'&&['switching','switched','installed'].includes(change.state))return 'saved';
      return await this.installed(directory) ? 'installed' : 'saved';
    } catch { return 'unreadable'; }
  }
  async list() {
    const result=[];this.roots=new Map();
    let entries;
    try {await this.direct(this.root);entries=await fs.readdir(this.root,{withFileTypes:true});}
    catch(error) {if(error.code==='ENOENT')return result;throw error;}
    for(const entry of entries.filter(e=>e.isDirectory()&&(e.name==='private'||INSTANCE_DIRECTORY.test(e.name))).slice(0,128)) {
      const directory=path.join(this.root,entry.name);
      try {
        await this.direct(directory);
        const status=await this.summary(directory);
        if(status==='none')continue;
        const id=crypto.createHash('sha256').update('setup:'+directory).digest('hex').slice(0,24);
        let profile=null;
        try {const h=await metadata(path.join(directory,'hosts/hermes/current.json'));
          if(h?.schema==='daemon.companion-host-install.v1'&&/^[a-f0-9]{32}$/.test(h.installation_id||'')) {
            profile=await this.direct(path.join(directory,'hosts/hermes/installations',h.installation_id,'profile'));
          }
        }catch{/* A partial setup can exist before its host profile. */}
        let host='hermes';
        if(!profile)try {const h=await metadata(path.join(directory,'hosts/openclaw/current.json'));
          if(h?.schema==='daemon.openclaw-first.v1'&&/^[a-f0-9]{32}$/.test(h.installation_id||'')){
            host='openclaw'; profile=await this.direct(path.join(directory,'hosts/openclaw/installations',h.installation_id,'profile'));
          }
        }catch{/* The host may still be downloading. */}
        const info=await fs.stat(directory);
        result.push({id,status,profile,host,createdAt:new Date(info.birthtimeMs||info.mtimeMs).toISOString()});
        this.roots.set(id,directory);
      }catch{/* Never traverse redirected installation roots. */}
    }
    return result.sort((a,b)=>b.createdAt.localeCompare(a.createdAt));
  }
  async resolve(id) {
    if(typeof id!=='string'||!/^[a-f0-9]{24}$/.test(id))throw new Error('Refresh the installation list first.');
    await this.list();const directory=this.roots.get(id);
    if(!directory)throw new Error('Refresh the installation list first.');
    return this.direct(directory);
  }
  async select(id) {
    const directory=await this.resolve(id);
    if(await this.summary(directory)!=='saved')throw new Error('This installation cannot be resumed.');
    await this.publish(directory);return directory;
  }
  async publish(directory) {
    const temporary=this.file+'.'+crypto.randomUUID()+'.tmp';
    try {
      await this.direct(this.root);await this.direct(directory);
      await fs.writeFile(temporary,JSON.stringify({schema:1,directory:path.basename(directory)})+'\n',{mode:0o600,flag:'wx'});
      await fs.rename(temporary,this.file);
    } finally {await fs.unlink(temporary).catch(()=>{});}
  }
  async installed(directory) {
    // Display hint from the existing transaction receipt, not a health check or
    // authority to control a service. Never traverse other retained profiles.
    const first=await metadata(path.join(directory,'hosts/openclaw/current.json'));
    if(first!==null) {
      if(first.schema!=='daemon.openclaw-first.v1'||!/^[a-f0-9]{32}$/.test(first.installation_id||''))throw new Error('Invalid OpenClaw receipt.');
      const attempt=path.join(directory,'hosts/openclaw/installations',first.installation_id);
      const receipt=await metadata(path.join(attempt,'daemon-setup.json'));
      if(receipt===null)return false;
      if(receipt.schema!=='daemon.private-setup.v1'||!/^[A-Za-z0-9_]{5,32}$/.test(receipt.username||'')||!/^[1-9][0-9]{0,19}$/.test(receipt.user_id||''))throw new Error('Invalid setup receipt.');
      const owner=await metadata(path.join(attempt,'owner.json'));
      if(owner?.status!=='installed'||owner.installation_id!==first.installation_id||owner.revision!==first.acquisition)throw new Error('Invalid host ownership.');
      await this.direct(path.join(attempt,'profile'));
      if(!isDeepStrictEqual(await metadata(path.join(attempt,'daemon/.daemon-harness-installation.json')),{schema:'daemon.companion-installation-scope.v1',host:first}))throw new Error('Invalid setup ownership.');
      return true;
    }
    const root = path.join(directory,'hosts','hermes');
    const host = await metadata(path.join(root,'current.json'));
    if(host === null)return false;
    if(!host || Object.keys(host).sort().join(',') !== 'installation_id,revision,schema,version' ||
        host.schema !== 'daemon.companion-host-install.v1' ||
        typeof host.installation_id !== 'string' || !/^[0-9a-f]{32}$/.test(host.installation_id) ||
        typeof host.version !== 'string' || !host.version || typeof host.revision !== 'string' || !host.revision)
      throw new Error('Invalid host receipt.');
    const attempt = path.join(root,'installations',host.installation_id);
    const receipt = await metadata(path.join(attempt,'daemon-setup.json'));
    if(receipt === null)return false;
    if(!receipt || receipt.schema !== 'daemon.private-setup.v1' ||
        typeof receipt.username !== 'string' || !/^[A-Za-z0-9_]{5,32}$/.test(receipt.username) ||
        typeof receipt.user_id !== 'string' || !/^[1-9][0-9]{0,19}$/.test(receipt.user_id) ||
        !isDeepStrictEqual(await metadata(path.join(attempt,'owner.json')),host))
      throw new Error('Invalid setup receipt.');
    await this.direct(path.join(attempt,'profile'));
    await this.direct(path.join(attempt,'daemon'));
    if(!isDeepStrictEqual(await metadata(path.join(attempt,'daemon','.daemon-harness-installation.json')),
        {schema:'daemon.companion-installation-scope.v1',host}))
      throw new Error('Invalid setup ownership.');
    return true;
  }
  async create() {
    await this.direct(this.root,true);
    const directory=await fs.mkdtemp(path.join(this.root,'instance-'));
    const temporary=this.file+'.'+crypto.randomUUID()+'.tmp';
    try {
      await this.direct(directory);
      await fs.writeFile(temporary,JSON.stringify({schema:1,directory:path.basename(directory)})+'\n',{mode:0o600,flag:'wx'});
      // Rename replaces the pointer itself, never a symlink target or old install.
      await fs.rename(temporary,this.file);
      return directory;
    } catch(error) {
      await fs.unlink(temporary).catch(()=>{});
      await fs.rmdir(directory).catch(()=>{});
      throw error;
    }
  }
}
module.exports={SetupHomes,INSTANCE_DIRECTORY};
