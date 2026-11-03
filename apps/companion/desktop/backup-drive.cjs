'use strict';
// Drive stores encrypted archives only. Paths and OAuth tokens never enter UI.
const crypto=require('node:crypto');const fs=require('node:fs/promises');const path=require('node:path');
const {boundedJSON}=require('./backup-google.cjs');const {MAGIC,MAX_BYTES}=require('./backup-crypto.cjs');
const API='https://www.googleapis.com/drive/v3/';
const UPLOAD='https://www.googleapis.com/upload/drive/v3/files';
const MARKER='daemons-portable-memory-v1';const CHUNK=8*1024*1024;
function id(value){if(typeof value!=='string'||!/^[a-zA-Z0-9_-]{1,200}$/.test(value))throw new Error('drive.invalidId');return value;}
function endpoint(value){const u=new URL(value);if(u.origin!=='https://www.googleapis.com'||u.username||u.password||u.hash||
 (!u.pathname.startsWith('/drive/v3/')&&u.pathname!=='/upload/drive/v3/files'))throw new Error('drive.unsafeEndpoint');return u.href;}
function session(value){const u=new URL(endpoint(value));if(u.pathname!=='/upload/drive/v3/files'||u.searchParams.get('uploadType')!=='resumable'||!u.searchParams.get('upload_id'))throw new Error('drive.unsafeSession');return u.href;}
const fields='id,name,size,md5Checksum,createdTime,appProperties,mimeType';
// What a backup says about itself, readable from any computer with the same
// sign-in. The daemon key groups automatic backups for rotation only.
function labels({kind='manual',daemon='',name=''}={}) {
 if(!['manual','automatic'].includes(kind)||!/^[a-f0-9]{0,40}$/.test(daemon))throw new Error('drive.invalidLabels');
 return {daemons_format:MARKER,daemons_kind:kind,daemons_daemon:daemon,daemons_name:String(name).slice(0,40)};
}
function described(f){
 const a=f.appProperties||{};
 return {id:id(f.id),createdAt:f.createdTime,bytes:checkedSize(f.size),checksum:checkedDigest(f.md5Checksum),
  kind:a.daemons_kind==='automatic'?'automatic':'manual',daemon:/^[a-f0-9]{1,40}$/.test(a.daemons_daemon||'')?a.daemons_daemon:'',name:typeof a.daemons_name==='string'?a.daemons_name.slice(0,40):''};
}
class Drive {
 constructor({token,fetchImpl=fetch,sleep=ms=>new Promise(r=>setTimeout(r,ms))}){this.token=token;this.fetch=fetchImpl;this.sleep=sleep;}
 async request(url,options={},signal){
  endpoint(url);
  for(let auth=0;auth<2;auth++){
   if(signal?.aborted)throw new Error('drive.cancelled');
   const token=await this.token({refresh:auth===1,signal});let response;
   try{response=await this.fetch(url,{...options,redirect:'error',signal:AbortSignal.any([AbortSignal.timeout(60000),...(signal?[signal]:[])]),
      headers:{...options.headers,Authorization:'Bearer '+token}});}
   catch{throw new Error(signal?.aborted?'drive.cancelled':'drive.network');}
   if(response.status===401){await response.body?.cancel();if(auth===0)continue;throw new Error('google.signInAgain');}
   return response;
  }
 }
 async json(url,options={},signal){
  const response=await this.request(url,options,signal);
  if(!response.ok){await response.body?.cancel();throw new Error(response.status===403?'drive.permissionOrQuota':response.status===404?'drive.notFound':'drive.requestFailed');}
  return boundedJSON(response);
 }
 async list(query,signal){
  const files=[];let page='';const seen=new Set();
  for(let n=0;n<100;n++){
   const u=new URL(API+'files');u.search=new URLSearchParams({q:query,pageSize:'100',fields:`nextPageToken,files(${fields})`,spaces:'drive',...(page?{pageToken:page}:{})});
   const value=await this.json(u.href,{},signal);if(!Array.isArray(value.files))throw new Error('drive.invalidResponse');
   files.push(...value.files);if(!value.nextPageToken)return files;
   if(typeof value.nextPageToken!=='string'||value.nextPageToken.length>4096||seen.has(value.nextPageToken))throw new Error('drive.invalidPagination');
   page=value.nextPageToken;seen.add(page);
  }
  throw new Error('drive.tooManyBackups');
 }
 async folder(name,role,parent=null,signal){
  if(!['Daemons','Backups'].includes(name)||!['root','backups'].includes(role))throw new Error('drive.invalidFolder');
  const query=`trashed = false and mimeType = 'application/vnd.google-apps.folder' and appProperties has { key='daemons_role' and value='${role}' }`+(parent?` and '${id(parent)}' in parents`:'');
  const matches=await this.list(query,signal);
  if(matches.length>1)throw new Error('drive.duplicateFolders');
  if(matches.length===1)return id(matches[0].id);
  const value=await this.json(API+'files?fields=id',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,
    mimeType:'application/vnd.google-apps.folder',appProperties:{daemons_role:role},...(parent?{parents:[parent]}:{})})},signal);
  return id(value.id);
 }
 async backups(signal){
  const files=await this.list(`trashed = false and appProperties has { key='daemons_format' and value='${MARKER}' }`,signal);
  return files.map(described).sort((a,b)=>String(b.createdAt).localeCompare(String(a.createdAt)));
 }
 // Deletes one automatic backup of the given daemon, after checking it is one.
 async remove(fileId,daemon,signal){
  const meta=await this.json(API+'files/'+id(fileId)+'?fields='+encodeURIComponent(fields),{},signal);
  const item=described(meta);
  if(meta.appProperties?.daemons_format!==MARKER||item.kind!=='automatic'||!daemon||item.daemon!==daemon)throw new Error('drive.notAutomatic');
  const response=await this.request(API+'files/'+fileId,{method:'DELETE'},signal);
  await response.body?.cancel();
  if(!response.ok&&response.status!==404)throw new Error('drive.requestFailed');
 }
 async upload(file,{signal,progress=()=>{},resume=null,checkpoint=async()=>{},label={}}={}){
  const info=await fs.lstat(file);
  if(!path.isAbsolute(file)||await fs.realpath(file)!==file||!info.isFile()||info.nlink!==1||info.size>MAX_BYTES||info.size<MAGIC.length+44)throw new Error('drive.invalidArchive');
  const handle=await fs.open(file,'r');
  try{
   const magic=Buffer.alloc(MAGIC.length);await handle.read(magic,0,magic.length,0);if(!magic.equals(MAGIC))throw new Error('drive.encryptedArchiveRequired');
   const checksum=await fileDigest(handle,info.size);let job;
   if(resume){
    if(resume.size!==info.size||resume.md5!==checksum)throw new Error('drive.resumeMismatch');
    job={id:id(resume.id),size:info.size,md5:checksum,url:resume.url?session(resume.url):null};
   } else {
    const root=await this.folder('Daemons','root',null,signal),folder=await this.folder('Backups','backups',root,signal);
    const generated=await this.json(API+'files/generateIds?count=1&space=drive&type=files',{},signal);
    if(!Array.isArray(generated.ids)||generated.ids.length!==1)throw new Error('drive.invalidResponse');
    job={id:id(generated.ids[0]),size:info.size,md5:checksum,url:null};
    // Persist the allocated ID before starting. It makes final-response loss
    // recoverable without creating a second backup on the next attempt.
    await checkpoint(job);
    const response=await this.request(UPLOAD+'?uploadType=resumable&fields='+encodeURIComponent(fields),{method:'POST',
      headers:{'Content-Type':'application/json','X-Upload-Content-Type':'application/octet-stream','X-Upload-Content-Length':String(info.size)},
      body:JSON.stringify({id:job.id,name:(label.name?String(label.name).replace(/[^\p{L}\p{N} _-]/gu,'').slice(0,40)+'-':'daemons-')+new Date().toISOString().replace(/[:.]/g,'-')+'.daemonbackup',parents:[folder],
        mimeType:'application/octet-stream',appProperties:labels(label)})},signal);
    if(!response.ok){await response.body?.cancel();throw new Error('drive.uploadStartFailed');}
    job.url=session(response.headers.get('location'));await response.body?.cancel();await checkpoint(job);
   }
   let offset=0,failures=0,probe=Boolean(resume);
   for(let operations=0;operations<Math.ceil(info.size/CHUNK)+64;operations++){
    if(signal?.aborted)throw new Error('drive.cancelled');
    if(!job.url){const result=await this.verify(job,signal);await checkpoint(null);return result;}
    let response,sent=offset;
    try{
     // Chromium sets Content-Length itself and refuses requests that set it.
     if(probe){response=await this.request(job.url,{method:'PUT',headers:{'Content-Range':`bytes */${info.size}`}},signal);}
     else{
      const length=Math.min(CHUNK,info.size-offset),buffer=Buffer.alloc(length);
      if(length<=0)throw new Error('drive.invalidRange');
      if((await handle.read(buffer,0,length,offset)).bytesRead!==length)throw new Error('drive.sourceChanged');
      sent=offset+length;
      response=await this.request(job.url,{method:'PUT',headers:{'Content-Type':'application/octet-stream',
        'Content-Range':`bytes ${offset}-${sent-1}/${info.size}`},body:buffer},signal);
     }
    }catch(error){
     if(error.message!=='drive.network'||++failures>5)throw error;
     await this.sleep(Math.min(1000*2**failures,10000));probe=true;continue;
    }
    if(response.status===200||response.status===201){await response.body?.cancel();const result=await this.verify(job,signal);await checkpoint(null);progress({bytes:info.size,total:info.size});return result;}
    if(response.status===308){
     const range=response.headers.get('range');await response.body?.cancel();
     if(range!==null&&!/^bytes=0-\d+$/.test(range))throw new Error('drive.invalidRange');
     const next=range===null?0:Number(range.slice(8))+1;
     if(!Number.isSafeInteger(next)||next<offset||next>info.size||(!probe&&next>sent))throw new Error('drive.invalidRange');
     if(next===offset&&++failures>5)throw new Error('drive.uploadStalled');
     if(next>offset)failures=0;offset=next;probe=offset===info.size;progress({bytes:offset,total:info.size});continue;
    }
    await response.body?.cancel();
    if(response.status===404||response.status===410){const result=await this.verify(job,signal);await checkpoint(null);return result;}
    if((response.status>=500||response.status===429)&&++failures<=5){await this.sleep(Math.min(1000*2**failures,10000));probe=true;continue;}
    throw new Error(response.status===403?'drive.permissionOrQuota':'drive.uploadFailed');
   }
   throw new Error('drive.uploadStalled');
  }finally{await handle.close();}
 }
 async verify(job,signal){
  const value=await this.json(API+'files/'+id(job.id)+'?fields='+encodeURIComponent(fields),{},signal);
  if(value.id!==job.id||checkedSize(value.size)!==job.size||checkedDigest(value.md5Checksum)!==job.md5||value.appProperties?.daemons_format!==MARKER)throw new Error('drive.integrity');
  return {id:job.id,bytes:job.size,checksum:job.md5};
 }
 async download(fileId,destination,{signal,progress=()=>{}}={}){
  id(fileId);if(!path.isAbsolute(destination)||await fs.realpath(path.dirname(destination))!==path.dirname(destination))throw new Error('drive.unsafeDestination');
  const meta=await this.json(API+'files/'+fileId+'?fields='+encodeURIComponent(fields),{},signal);
  if(meta.appProperties?.daemons_format!==MARKER)throw new Error('drive.invalidArchive');
  const size=checkedSize(meta.size),checksum=checkedDigest(meta.md5Checksum);
  const directory=await fs.mkdtemp(path.join(path.dirname(destination),'.daemons-download-'));await fs.chmod(directory,0o700);
  try{
   const file=path.join(directory,'archive'),output=await fs.open(file,'wx',0o600);let bytes=0;const hash=crypto.createHash('md5');
   try{
    const response=await this.request(API+'files/'+fileId+'?alt=media',{},signal);
    if(!response.ok){await response.body?.cancel();throw new Error('drive.downloadFailed');}
    for await(const chunk of response.body){bytes+=chunk.length;if(bytes>size)throw new Error('drive.integrity');hash.update(chunk);await output.writeFile(chunk);progress({bytes,total:size});}
    if(bytes!==size||hash.digest('hex')!==checksum)throw new Error('drive.integrity');await output.sync();
   }finally{await output.close();}
   await fs.link(file,destination);return {bytes,size};
  }finally{await fs.rm(directory,{recursive:true,force:true});}
 }
}
function checkedSize(raw){if(typeof raw!=='string'||!/^\d+$/.test(raw)||!Number.isSafeInteger(Number(raw))||Number(raw)<1||Number(raw)>MAX_BYTES)throw new Error('drive.invalidSize');return Number(raw);}
function checkedDigest(raw){if(typeof raw!=='string'||!/[a-f0-9]{32}/.test(raw)||raw.length!==32)throw new Error('drive.invalidChecksum');return raw;}
async function fileDigest(handle,size){let position=0;const hash=crypto.createHash('md5'),buffer=Buffer.alloc(CHUNK);while(position<size){const {bytesRead}=await handle.read(buffer,0,Math.min(CHUNK,size-position),position);if(!bytesRead)throw new Error('drive.sourceChanged');hash.update(buffer.subarray(0,bytesRead));position+=bytesRead;}return hash.digest('hex');}
module.exports={Drive,endpoint,session,labels,MARKER,CHUNK};
