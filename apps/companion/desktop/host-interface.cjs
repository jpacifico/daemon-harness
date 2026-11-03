'use strict';
// Main-process only. Credentials and authenticated URLs never cross renderer IPC.
const fs=require('node:fs/promises');
const path=require('node:path');
const crypto=require('node:crypto');
const {spawn,execFile}=require('node:child_process');
const {promisify}=require('node:util');
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function stop(child){
 if(!child||child.exitCode!==null||child.signalCode!==null)return;
 if(process.platform==='win32')await promisify(execFile)('taskkill.exe',['/PID',String(child.pid),'/T','/F'],{windowsHide:true}).catch(()=>{});
 else {const exited=new Promise(r=>child.once('exit',r));child.kill('SIGTERM');await Promise.race([exited,sleep(2000)]);if(child.exitCode===null&&child.signalCode===null)child.kill('SIGKILL');}
}
async function verifyAssets(root,revision){
 const manifest=JSON.parse(await fs.readFile(path.join(root,'companion-assets.json'),'utf8'));
 if(manifest.revision!==revision||!manifest.files?.['index.html'])throw new Error('hostInterface.assets');
 for(const [name,digest] of Object.entries(manifest.files)){
  if(path.isAbsolute(name)||name.split('/').includes('..')||name.includes('\\'))throw new Error('hostInterface.assets');
  const file=path.join(root,name),info=await fs.lstat(file);
  if(!info.isFile()||info.isSymbolicLink()||crypto.createHash('sha256').update(await fs.readFile(file)).digest('hex')!==digest)throw new Error('hostInterface.assets');
 }
}
function localUrl(value){
 const url=new URL(value);
 if(url.protocol!=='http:'||url.hostname!=='127.0.0.1'||url.username||url.password||!url.port||Number(url.port)<1024)throw new Error('hostInterface.unavailable');
 return url;
}
class HostInterface {
 constructor({instances,run,open,assets,stateRoot,spawnProcess=spawn,probe=fetch,timeout=120000}){
  Object.assign(this,{instances,run,open,assets,stateRoot,spawnProcess,probe,timeout});this.sessions=new Map();this.busy=false;
 }
 async launch(id){
  if(this.busy)throw new Error('hostInterface.busy');this.busy=true;
  try {
   await this.instances.refresh();const row=this.instances.rows.get(id);
   if(!row||row.problem)throw new Error('hostInterface.unavailable');
   if(row.host==='openclaw'&&row.state!=='running')throw new Error('hostInterface.stopped');
   const plan=await this.run({host:row.host,profile:row.profile,base:this.instances.base});
   if(plan.profile!==row.profile||plan.host!==row.host)throw new Error('hostInterface.unavailable');
   if(plan.host==='openclaw'){
    const url=localUrl(plan.url);const probe=new URL(url);probe.hash='';
    const reply=await this.probe(probe,{redirect:'error',signal:AbortSignal.timeout(5000)}).catch(()=>null);
    if(!reply?.ok)throw new Error('hostInterface.stopped');
    await this.open(url.href);return {opened:true};
   }
   const previous=this.sessions.get(id);
   if(previous&&previous.profile===row.profile&&previous.child.exitCode===null&&previous.child.signalCode===null){await this.open(previous.url);return {opened:true};}
   await verifyAssets(this.assets,plan.revision).catch(()=>{throw new Error('hostInterface.assets');});
   await fs.mkdir(this.stateRoot,{recursive:true,mode:0o700});
   const scratch=await fs.mkdtemp(path.join(this.stateRoot,'dashboard-'));
   const ready=path.join(scratch,'ready.json');
   let child;
   try {
    const env={...plan.environment,HERMES_HOME:row.profile,HERMES_WEB_DIST:this.assets,HERMES_DESKTOP_READY_FILE:ready,PYTHONUNBUFFERED:'1'};
    // Host native routing must never attach to another machine/profile dashboard.
    const args=['-B','-c','from hermes_cli.main import main;main()','dashboard','--isolated','--host','127.0.0.1','--port','0','--no-open','--skip-build'];
    child=this.spawnProcess(plan.executable,args,{cwd:plan.cwd,env,stdio:'ignore',windowsHide:true});
    let spawnError=false;child.once('error',()=>{spawnError=true;});
    const start=Date.now();let port;
    while(Date.now()-start<this.timeout){
     if(spawnError||child.exitCode!==null||child.signalCode!==null)throw new Error('hostInterface.failed');
     try {const data=JSON.parse(await fs.readFile(ready,'utf8'));if(Number.isInteger(data.port)&&data.port>=1024&&data.port<=65535){port=data.port;break;}}catch{/* The owned child has not announced readiness yet. */}
     await sleep(150);
    }
    if(!port)throw new Error('hostInterface.timeout');
    const url=`http://127.0.0.1:${port}/`;
    const reply=await this.probe(url,{redirect:'error',signal:AbortSignal.timeout(5000)}).catch(()=>null);
    if(!reply?.ok)throw new Error('hostInterface.failed');
    await this.open(url);
    this.sessions.set(id,{child,url,profile:row.profile,scratch});
    child.once('exit',()=>{if(this.sessions.get(id)?.child===child)this.sessions.delete(id);fs.rm(scratch,{recursive:true,force:true}).catch(()=>{});});
    return {opened:true};
   } catch(error){await stop(child);await fs.rm(scratch,{recursive:true,force:true});throw error;}
  }finally{this.busy=false;}
 }
 async closeFor(id){const session=this.sessions.get(id);if(session){await stop(session.child);await fs.rm(session.scratch,{recursive:true,force:true});this.sessions.delete(id);}}
 async close(){await Promise.all([...this.sessions.values()].map(async s=>{await stop(s.child);await fs.rm(s.scratch,{recursive:true,force:true});}));this.sessions.clear();}
}
module.exports={HostInterface,verifyAssets,localUrl};
