'use strict';
// Electron safeStorage delegates to Keychain on macOS and DPAPI on Windows.
// No Linux basic_text fallback and no credential material in renderer storage.
const fs=require('node:fs/promises');const path=require('node:path');const crypto=require('node:crypto');
class ProtectedStore {
 // One protected file per secret: the Google sign-in, or one daemon's backup key.
 constructor(directory,safeStorage,platform=process.platform,name='google-drive.bin'){if(!/^[a-z0-9-]{1,80}\.bin$/.test(name))throw new Error('google.unsafeStorage');this.directory=path.resolve(directory);this.file=path.join(this.directory,name);this.crypto=safeStorage;this.platform=platform;}
 supported(){if(!['darwin','win32'].includes(this.platform)||!this.crypto.isEncryptionAvailable())throw new Error('google.secureStorageUnavailable');}
 async root(create=false){this.supported();if(create)await fs.mkdir(this.directory,{recursive:true,mode:0o700});if(await fs.realpath(this.directory)!==this.directory)throw new Error('google.unsafeStorage');}
 async get(){
  this.supported();try{await this.root();const info=await fs.lstat(this.file);
   if(!info.isFile()||info.isSymbolicLink()||info.nlink!==1||info.size>65536)throw new Error('google.unsafeStorage');
   return JSON.parse(this.crypto.decryptString(await fs.readFile(this.file)));
  }catch(error){if(error.code==='ENOENT')return null;throw new Error('google.secureStorageUnavailable');}
 }
 async put(value){
  await this.root(true);const temporary=path.join(this.directory,'.credentials-'+crypto.randomUUID());
  try{const raw=JSON.stringify(value);if(raw.length>32768)throw new Error('google.invalidToken');
   await fs.writeFile(temporary,this.crypto.encryptString(raw),{flag:'wx',mode:0o600});await fs.rename(temporary,this.file);
  }finally{await fs.unlink(temporary).catch(()=>{});}
 }
 async delete(){try{await this.root();await fs.unlink(this.file);}catch(error){if(error.code!=='ENOENT')throw error;}}
}
module.exports={ProtectedStore};
