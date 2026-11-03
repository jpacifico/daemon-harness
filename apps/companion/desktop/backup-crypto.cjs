'use strict';
// File encryption is confined to the main process. No password is persisted;
// automatic backups keep only a derived key, in the system's protected store.
const crypto = require('node:crypto');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');
const {pipeline} = require('node:stream/promises');
const {promisify} = require('node:util');
const scrypt = promisify(crypto.scrypt);
const MAGIC = Buffer.from('DAEMONS-BACKUP\x00\x01', 'binary');
const SALT = 16, NONCE = 12, TAG = 16;
const HEADER = MAGIC.length + SALT + NONCE;
const MAX_BYTES = 9 * 1024 * 1024 * 1024;
const PARAMETERS = Object.freeze({N:32768,r:8,p:1,maxmem:64*1024*1024});
async function direct(file, exists=true) {
  if(!path.isAbsolute(file)||path.normalize(file)!==file||await fsp.realpath(path.dirname(file))!==path.dirname(file))throw new Error('backup.unsafePath');
  if(exists) {
    const info=await fsp.lstat(file);
    if(!info.isFile()||info.isSymbolicLink()||info.nlink!==1||info.size>MAX_BYTES||await fsp.realpath(file)!==file)throw new Error('backup.unsafeFile');
    return info;
  }
}
async function key(password,salt) {
  if(typeof password!=='string'||password.length<12||Buffer.byteLength(password)>1024)throw new Error('backup.passwordLength');
  return scrypt(password,salt,32,PARAMETERS);
}
// A key for automatic backups: the same scrypt derivation, kept with its salt so
// every file it writes still opens with the password alone.
async function deriveKey(password) {
  const salt=crypto.randomBytes(SALT);
  return {salt:salt.toString('base64'),key:(await key(password,salt)).toString('base64')};
}
function storedKey(value) {
  const salt=Buffer.from(value?.salt||'','base64'),derived=Buffer.from(value?.key||'','base64');
  if(salt.length!==SALT||derived.length!==32)throw new Error('backup.keyUnavailable');
  return {salt,derived};
}
async function transfer(source,destination,password,{decrypt=false,signal,keyMaterial=null}={}) {
  const info=await direct(source);await direct(destination,false);
  if(source===destination)throw new Error('backup.destinationExists');
  const directory=await fsp.mkdtemp(path.join(path.dirname(destination),'.daemons-crypto-'));
  await fsp.chmod(directory,0o700);
  const temporary=path.join(directory,'archive');
  let derived;
  try {
    let header,cipher,start=0,end=info.size-1;
    if(decrypt) {
      if(info.size<HEADER+TAG)throw new Error('backup.invalidArchive');
      const handle=await fsp.open(source,'r');
      try {
        header=Buffer.alloc(HEADER);const tag=Buffer.alloc(TAG);
        if((await handle.read(header,0,HEADER,0)).bytesRead!==HEADER||(await handle.read(tag,0,TAG,info.size-TAG)).bytesRead!==TAG||!header.subarray(0,MAGIC.length).equals(MAGIC))throw new Error('backup.invalidArchive');
        derived=await key(password,header.subarray(MAGIC.length,MAGIC.length+SALT));
        cipher=crypto.createDecipheriv('aes-256-gcm',derived,header.subarray(MAGIC.length+SALT),{authTagLength:TAG});
        cipher.setAAD(header);cipher.setAuthTag(tag);start=HEADER;end=info.size-TAG-1;
      }finally{await handle.close();}
    } else {
      const saved=keyMaterial?storedKey(keyMaterial):null;
      header=Buffer.concat([MAGIC,saved?saved.salt:crypto.randomBytes(SALT),crypto.randomBytes(NONCE)]);
      derived=saved?saved.derived:await key(password,header.subarray(MAGIC.length,MAGIC.length+SALT));
      cipher=crypto.createCipheriv('aes-256-gcm',derived,header.subarray(MAGIC.length+SALT),{authTagLength:TAG});
      cipher.setAAD(header);
    }
    await fsp.writeFile(temporary,decrypt?Buffer.alloc(0):header,{mode:0o600,flag:'wx'});
    const {Readable}=require('node:stream');
    const input=end<start?Readable.from([]):fs.createReadStream(source,{start,end});
    try {await pipeline(input,cipher,fs.createWriteStream(temporary,{flags:'a',mode:0o600}),{signal});}
    catch(error){if(signal?.aborted)throw new Error('backup.cancelled');if(decrypt)throw new Error('backup.authenticationFailed');throw error;}
    if(!decrypt)await fsp.appendFile(temporary,cipher.getAuthTag());
    const after=await fsp.stat(source);
    if(after.size!==info.size||after.mtimeMs!==info.mtimeMs||after.ino!==info.ino)throw new Error('backup.sourceChanged');
    if(signal?.aborted)throw new Error('backup.cancelled');
    const handle=await fsp.open(temporary,'r+');try{await handle.sync();}finally{await handle.close();}
    // link() is exclusive: a concurrent destination is never overwritten.
    await fsp.link(temporary,destination);
    return {bytes:(await fsp.stat(destination)).size};
  } finally {
    derived?.fill(0);
    await fsp.rm(directory,{recursive:true,force:true});
  }
}
module.exports={deriveKey,encryptWithKey:(a,b,k,o)=>transfer(a,b,null,{...o,keyMaterial:k}),encrypt:(a,b,p,o)=>transfer(a,b,p,o),decrypt:(a,b,p,o)=>transfer(a,b,p,{...o,decrypt:true}),MAGIC,HEADER,MAX_BYTES};
