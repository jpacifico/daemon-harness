'use strict';
// Technical registrations only. Never inspect personal notes or execute a host.
const fs=require('node:fs/promises');
const path=require('node:path');
const crypto=require('node:crypto');
const hash=value=>crypto.createHash('sha256').update(value).digest('hex');
const reference=profile=>hash(profile).slice(0,32);
// Windows paths written by the private runtime use the extended \\?\ spelling.
// Compare one plain spelling, case-insensitively on Windows.
function plain(value) {
  if(typeof value!=='string'||process.platform!=='win32')return value;
  if(value.startsWith('\\\\?\\UNC\\'))return '\\\\'+value.slice(8);
  return value.startsWith('\\\\?\\')?value.slice(4):value;
}
const key=value=>{const p=plain(value);return process.platform==='win32'&&typeof p==='string'?path.normalize(p).toLowerCase():p;};
const samePath=(a,b)=>typeof a==='string'&&typeof b==='string'&&key(a)===key(b);
async function direct(value,directory=false) {
  value=plain(value);
  if(typeof value!=='string'||!path.isAbsolute(value)||path.normalize(value)!==value||!samePath(await fs.realpath(value),value))throw new Error('Invalid association path.');
  const info=await fs.lstat(value);
  if(info.isSymbolicLink()||(directory?!info.isDirectory():!info.isFile()))throw new Error('Invalid association path.');
  return value;
}
async function registration(state,ref) {
  if(!/^[a-f0-9]{32}$/.test(ref))throw new Error('Invalid association reference.');
  await direct(state,true);
  const root=await direct(path.join(state,'host-bindings'),true);
  const file=await direct(path.join(root,ref+'.json'));
  const info=await fs.stat(file);
  if(info.size>16384||info.nlink!==1)throw new Error('Invalid association metadata.');
  const raw=await fs.readFile(file,'utf8');
  if(Buffer.byteLength(raw)>16384)throw new Error('Oversized association metadata.');
  const data=JSON.parse(raw);
  if(data.schema!=='daemon.host-execution.v1'||data.host_ref!==ref||data.status!=='active'||data.host!=='openclaw'||typeof data.profile!=='string'||reference(data.profile)!==ref||typeof data.user_id!=='string'||!data.user_id)throw new Error('Invalid association metadata.');
  return data;
}
async function resolve(profile,environment,plugin) {
  if(!Object.hasOwn(environment,'DAEMON_CORE_HOST_REF'))return null;
  const data=await registration(environment.DAEMON_CORE_STATE_ROOT,environment.DAEMON_CORE_HOST_REF);
  if(data.profile!==profile||data.user_id!==plugin.userId||data.memory!==environment.DAEMON_CORE_MEMORY_ROOT)throw new Error('Association does not match this host.');
  await direct(profile,true);
  const memory=await direct(data.memory,true);
  const source=await direct(environment.DAEMON_CORE_SRC,true);
  return {memory,source};
}
async function assertNoDependents(state,profile,allowed=new Set()) {
  if(!state)return;
  await direct(state,true);
  const root=path.join(state,'host-bindings');
  let entries;
  try {await direct(root,true);entries=await fs.readdir(root);}catch(error){if(error.code==='ENOENT')return;throw error;}
  if(entries.length>128)throw new Error('Too many host registrations.');
  for(const name of entries) {
    if(!/^[a-f0-9]{32}\.json$/.test(name))throw new Error('Unknown host registration.');
    const data=await registration(state,name.slice(0,-5));
    // Even an unavailable destination still owns a dependency on the runtime.
    if(!samePath(data.profile,profile)&&![...allowed].some(p=>samePath(p,data.profile))){
      const error=new Error('Another host depends on this daemon.');error.dependent=true;throw error;
    }
  }
}
module.exports={resolve,assertNoDependents,reference,samePath,plain,key};
