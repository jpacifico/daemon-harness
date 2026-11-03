'use strict';
const {test}=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs/promises');const path=require('node:path');const os=require('node:os');const crypto=require('node:crypto');
const {Drive,endpoint,session,MARKER,CHUNK}=require('./backup-drive.cjs');const {encrypt}=require('./backup-crypto.cjs');
async function fixture(t){const dir=await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(),'daemon-drive-test-')));t.after(()=>fs.rm(dir,{recursive:true,force:true}));const plain=path.join(dir,'plain'),file=path.join(dir,'encrypted');await fs.writeFile(plain,Buffer.alloc(CHUNK+131,71));await encrypt(plain,file,'synthetic-backup-password');const bytes=await fs.readFile(file);return {dir,file,bytes,metadata:{id:'backup1',size:String(bytes.length),md5Checksum:crypto.createHash('md5').update(bytes).digest('hex'),appProperties:{daemons_format:MARKER}}};}
function server(f,{interrupt=false,lostFinal=false,unsafe=false}={}){
 let received=Buffer.alloc(0),broken=false,finalLost=false,probes=0;
 const fetchImpl=async(address,options)=>{
  // Like Chromium's network stack, refuse headers only the browser may set.
  for(const name of Object.keys(options.headers||{}))if(/^(content-length|host|connection|transfer-encoding|expect)$/i.test(name))throw new TypeError('net::ERR_INVALID_ARGUMENT');
  assert.equal(options.headers.Authorization,'Bearer test-access');assert.equal(options.redirect,'error');const url=new URL(address);
  if(url.pathname==='/drive/v3/files'&&options.method!=='POST'){
   const q=url.searchParams.get('q')||'';
   return Response.json({files:q.includes("value='root'")?[{id:'root'}]:q.includes("value='backups'")?[{id:'folder'}]:[f.metadata]});
  }
  if(url.pathname.endsWith('/generateIds'))return Response.json({ids:['backup1']});
  if(url.pathname==='/upload/drive/v3/files'&&options.method==='POST')return new Response('',{status:200,headers:{location:unsafe?'https://evil.example/upload':'https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=test'}});
  if(url.pathname==='/upload/drive/v3/files'){
   const range=options.headers['Content-Range'];
   if(range.startsWith('bytes */')){probes++;if(received.length===f.bytes.length)return Response.json(f.metadata);return new Response(null,{status:308,headers:received.length?{range:'bytes=0-'+(received.length-1)}:{}});}
   const start=Number(/bytes (\d+)-/.exec(range)[1]);assert.equal(start,received.length);
   received=Buffer.concat([received,options.body]);
   if(interrupt&&!broken){broken=true;throw new Error('connection lost after server accepted chunk');}
   if(received.length===f.bytes.length){if(lostFinal&&!finalLost){finalLost=true;throw new Error('lost final response');}return Response.json(f.metadata);}
   return new Response(null,{status:308,headers:{range:'bytes=0-'+(received.length-1)}});
  }
  if(url.pathname==='/drive/v3/files/backup1')return url.searchParams.get('alt')==='media'?new Response(f.bytes):Response.json(f.metadata);
  throw new Error('unexpected request');
 };
 return {fetchImpl,get received(){return received;},get probes(){return probes;}};
}
test('upload resumes after accepted chunk loses its response and verifies final remote digest',async t=>{
 const f=await fixture(t),remote=server(f,{interrupt:true,lostFinal:true}),checkpoints=[],progress=[];
 const drive=new Drive({token:async()=> 'test-access',fetchImpl:remote.fetchImpl,sleep:async()=>{}});
 const result=await drive.upload(f.file,{checkpoint:async v=>checkpoints.push(v),progress:p=>progress.push(p)});
 assert.equal(result.id,'backup1');assert.deepEqual(remote.received,f.bytes);assert.equal(remote.probes,2);assert.equal(checkpoints.at(-1),null);assert.equal(progress.at(-1).bytes,f.bytes.length);
 const out=path.join(f.dir,'downloaded');await drive.download('backup1',out);assert.deepEqual(await fs.readFile(out),f.bytes);
 assert.deepEqual((await new Drive({token:async()=> 'test-access',fetchImpl:remote.fetchImpl}).backups()).map(x=>x.id),['backup1']);
});
test('untrusted upload URL is refused before sending token or archive',async t=>{
 const f=await fixture(t),remote=server(f,{unsafe:true}),drive=new Drive({token:async()=> 'test-access',fetchImpl:remote.fetchImpl});
 await assert.rejects(drive.upload(f.file),/unsafeEndpoint/);assert.equal(remote.received.length,0);
 for(const url of ['http://www.googleapis.com/drive/v3/files','https://www.googleapis.com.evil/drive/v3/files','https://x@www.googleapis.com/drive/v3/files'])assert.throws(()=>endpoint(url));
 assert.throws(()=>session('https://www.googleapis.com/drive/v3/files'),/unsafeSession/);
});
test('corrupt downloads and existing files never replace a destination',async t=>{
 const f=await fixture(t),remote=server(f),drive=new Drive({token:async()=> 'test-access',fetchImpl:remote.fetchImpl});
 const destination=path.join(f.dir,'kept');await fs.writeFile(destination,'keep');await assert.rejects(drive.download('backup1',destination));assert.equal(await fs.readFile(destination,'utf8'),'keep');
 f.metadata.md5Checksum='0'.repeat(32);await assert.rejects(drive.download('backup1',path.join(f.dir,'new')),/integrity/);await assert.rejects(fs.stat(path.join(f.dir,'new')));
 assert.equal((await fs.readdir(f.dir)).some(x=>x.startsWith('.daemons-download-')),false);
});
test('expired access refreshes once, repeated page token and quotas produce bounded errors',async()=>{
 const calls=[];let n=0;
 const drive=new Drive({token:async args=>{calls.push(args.refresh);return 'test-access';},fetchImpl:async()=>++n===1?new Response('private',{status:401}):Response.json({files:[],nextPageToken:'repeat'})});
 await assert.rejects(drive.backups(),/invalidPagination/);assert.deepEqual(calls.slice(0,2),[false,true]);
 drive.fetch=async()=>new Response('sensitive provider text',{status:403});await assert.rejects(drive.backups(),/^Error: drive.permissionOrQuota$/);
});
test('resume verifies previously completed ID without uploading a duplicate',async t=>{
 const f=await fixture(t),remote=server(f),drive=new Drive({token:async()=> 'test-access',fetchImpl:remote.fetchImpl});
 const result=await drive.upload(f.file,{resume:{id:'backup1',size:f.bytes.length,md5:f.metadata.md5Checksum,url:null}});assert.equal(result.id,'backup1');assert.equal(remote.received.length,0);
 await assert.rejects(drive.upload(f.file,{resume:{id:'backup1',size:1,md5:f.metadata.md5Checksum,url:null}}),/resumeMismatch/);
});
