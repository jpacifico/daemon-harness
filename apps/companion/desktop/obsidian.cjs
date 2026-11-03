'use strict';
// Obsidian owns note files. Companion only registers their existing directory.
const fs=require('node:fs/promises'),{constants}=require('node:fs');
const path=require('node:path'),crypto=require('node:crypto');
const execFile=require('node:util').promisify(require('node:child_process').execFile);
const MAX_BYTES=262144,MAX_VAULTS=512;
const fail=key=>new Error('obsidian.'+key);
const object=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
function samePath(a,b,platform=process.platform){return platform==='win32'?a.toLowerCase()===b.toLowerCase():a===b;}

async function isObsidianRunning(application,{platform=process.platform,run=execFile,env=process.env}={}) {
  const options={encoding:'utf8',timeout:5000,maxBuffer:4*1024*1024,windowsHide:true};
  try {
    if(platform==='darwin') {
      const {stdout}=await run('/bin/ps',['-axww','-o','comm='],options);
      const processes=stdout.split(/\r?\n/).map(s=>s.trim()).filter(Boolean);
      if(!processes.length)throw new Error();
      const bundle=application.path.replace(/\/$/,'')+'/Contents/';
      // The app and its helpers count; app extensions under PlugIns do not.
      // macOS runs them on its own, such as the Finder's Quick Look preview.
      return processes.some(p=>(p.startsWith(bundle)&&!p.startsWith(bundle+'PlugIns/'))||/\/Contents\/MacOS\/Obsidian$/i.test(p)||path.basename(p).toLowerCase()==='obsidian');
    }
    if(platform==='win32') {
      const root=env.SystemRoot||env.SYSTEMROOT;
      if(!root||!path.win32.isAbsolute(root))throw new Error();
      const {stdout}=await run(path.win32.join(root,'System32','tasklist.exe'),['/FO','CSV','/NH'],options);
      const rows=stdout.split(/\r?\n/).filter(s=>s.trim());
      if(!rows.length)throw new Error();
      const names=rows.map(row=>{const m=/^"([^"]+)","[0-9]+",/.exec(row.trim());if(!m)throw new Error();return m[1].toLowerCase();});
      return names.some(name=>name==='obsidian.exe'||name===path.win32.basename(application.path).toLowerCase());
    }
  }catch{throw fail('processUnknown');}
  throw fail('processUnknown');
}

class Obsidian {
  constructor({appData,application,open,running=isObsidianRunning}){Object.assign(this,{appData,application,open,running});this.busy=false;}
  async app(){try {const info=await this.application('obsidian://choose-vault');if(info?.path)return info;}catch{}throw fail('unavailable');}
  async directory(){const parent=await fs.realpath(this.appData),dir=path.join(parent,'obsidian');
    try{const stat=await fs.lstat(dir);if(!stat.isDirectory()||stat.isSymbolicLink()||!samePath(await fs.realpath(dir),dir))throw fail('configUnsafe');}
    catch(e){if(e.code!=='ENOENT')throw fail('configUnsafe');}
    return dir;
  }
  async read() {
    const dir=await this.directory(),file=path.join(dir,'obsidian.json');let handle;
    try {
      const lst=await fs.lstat(file);
      if(!lst.isFile()||lst.isSymbolicLink()||lst.nlink!==1)throw fail('configUnsafe');
      handle=await fs.open(file,constants.O_RDONLY|(constants.O_NOFOLLOW||0));
      const stat=await handle.stat();
      if(!stat.isFile()||stat.size>MAX_BYTES||stat.ino!==lst.ino||stat.dev!==lst.dev)throw fail('configUnsafe');
      const buffer=Buffer.alloc(MAX_BYTES+1);const {bytesRead}=await handle.read(buffer,0,buffer.length,0);
      const end=await handle.stat();
      if(bytesRead>MAX_BYTES||bytesRead!==stat.size||end.mtimeMs!==stat.mtimeMs||end.size!==stat.size)throw fail('configChanged');
      const raw=buffer.subarray(0,bytesRead),data=JSON.parse(raw.toString('utf8'));
      if(!object(data)||(data.vaults!==undefined&&!object(data.vaults)))throw fail('configUnsafe');
      const entries=Object.entries(data.vaults||{});
      if(entries.length>MAX_VAULTS||entries.some(([id,v])=>!/^[a-f0-9]{16}$/.test(id)||!object(v)||typeof v.path!=='string'||!path.isAbsolute(v.path)))throw fail('configUnsafe');
      return {dir,file,raw,data,stat};
    }catch(e){if(e.code==='ENOENT'&&!handle)return {dir,file,raw:null,data:{}};if(e.message?.startsWith('obsidian.'))throw e;throw fail('configUnsafe');}
    finally{await handle?.close();}
  }
  async find(data,memory){
    for(const [id,vault] of Object.entries(data.vaults||{})){
      try{if(samePath(await fs.realpath(vault.path),memory))return id;}catch{}
    }
    return null;
  }
  async inspect(memory) {
    try{await this.app();}catch{return {available:false,registered:false};}
    let id=null;try{id=await this.find((await this.read()).data,await fs.realpath(memory));}catch{}
    return {available:true,registered:Boolean(id),...(id?{id}:{})};
  }
  async closed(application){if(await this.running(application))throw fail('closeFirst');}
  async unchanged(before){const current=await this.read();
    if(current.dir!==before.dir||(before.raw===null?current.raw!==null:!current.raw?.equals(before.raw)||current.stat.ino!==before.stat.ino||current.stat.dev!==before.stat.dev||current.stat.mtimeMs!==before.stat.mtimeMs))throw fail('configChanged');
  }
  async privateFile(file,bytes){const h=await fs.open(file,'wx',0o600);try{await h.writeFile(bytes);await h.sync();}finally{await h.close();}}
  async register(memory,application) {
    await this.closed(application);
    const before=await this.read();
    let id=await this.find(before.data,memory);if(id)return {id,added:false};
    if(Object.keys(before.data.vaults||{}).length>=MAX_VAULTS)throw fail('configUnsafe');
    await fs.mkdir(before.dir,{recursive:true,mode:0o700});
    if(await this.directory()!==before.dir)throw fail('configChanged');
    const lock=path.join(before.dir,'obsidian.daemons-register.lock');let lockHandle,temp;
    try {
      try{lockHandle=await fs.open(lock,'wx',0o600);}catch(e){if(e.code==='EEXIST')throw fail('registrationBusy');throw e;}
      await this.closed(application);await this.unchanged(before);
      do{id=crypto.randomBytes(8).toString('hex');}while(Object.hasOwn(before.data.vaults||{},id));
      const data={...before.data,vaults:{...before.data.vaults,[id]:{path:memory,ts:Date.now(),open:true}}};
      const bytes=Buffer.from(JSON.stringify(data));if(bytes.length>MAX_BYTES)throw fail('configUnsafe');
      if(before.raw!==null)await this.privateFile(path.join(before.dir,`obsidian.daemons-backup-${crypto.randomUUID()}.json`),before.raw);
      temp=path.join(before.dir,`.obsidian.daemons-${crypto.randomUUID()}.tmp`);
      await this.privateFile(temp,bytes);
      await this.closed(application);await this.unchanged(before);
      if(!samePath(await fs.realpath(memory),memory)||!(await fs.stat(memory)).isDirectory())throw fail('configChanged');
      // Obsidian does not share a transaction lock. The user must leave it closed
      // until this atomic replacement completes; do not claim full exclusion.
      if(before.raw===null){await fs.link(temp,before.file);await fs.unlink(temp);}else await fs.rename(temp,before.file);
      temp=null;
      return {id,added:true};
    }catch(e){if(e.message?.startsWith('obsidian.'))throw e;throw fail('registrationFailed');}
    finally{
      if(temp)await fs.unlink(temp).catch(()=>{});
      if(lockHandle){const held=await lockHandle.stat();await lockHandle.close();try{const current=await fs.lstat(lock);if(current.ino===held.ino&&current.dev===held.dev)await fs.unlink(lock);}catch{}}
    }
  }
  async launch(memory) {
    if(this.busy)throw fail('registrationBusy');this.busy=true;
    try {
      const application=await this.app();
      let target;try{target=await fs.realpath(memory);if(!(await fs.stat(target)).isDirectory())throw new Error();}catch{throw fail('memoryUnavailable');}
      const before=await this.read();let id=await this.find(before.data,target),added=false;
      if(!id)({id,added}=await this.register(target,application));
      try{await this.open('obsidian://open?vault='+encodeURIComponent(id));}catch{throw fail('openFailed');}
      return {opened:true,registered:true,added};
    }finally{this.busy=false;}
  }
}
module.exports={Obsidian,isObsidianRunning};
