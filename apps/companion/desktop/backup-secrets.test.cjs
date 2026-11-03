'use strict';
const {test}=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs/promises');const os=require('node:os');const path=require('node:path');const crypto=require('node:crypto');
const {ProtectedStore}=require('./backup-secrets.cjs');
test('protected storage delegates encryption, bounds reads and refuses plaintext fallback',async t=>{
 const directory=await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(),'daemon-secret-test-')));t.after(()=>fs.rm(directory,{recursive:true,force:true}));
 const key=crypto.randomBytes(32),iv=crypto.randomBytes(12);
 const safe={isEncryptionAvailable:()=>true,encryptString:value=>{const c=crypto.createCipheriv('aes-256-gcm',key,iv);const bytes=Buffer.concat([c.update(value,'utf8'),c.final()]);return Buffer.concat([bytes,c.getAuthTag()]);},decryptString:bytes=>{const c=crypto.createDecipheriv('aes-256-gcm',key,iv);c.setAuthTag(bytes.subarray(-16));return Buffer.concat([c.update(bytes.subarray(0,-16)),c.final()]).toString('utf8');}};
 const store=new ProtectedStore(directory,safe,'darwin');assert.equal(await store.get(),null);
 await store.put({refresh_token:'synthetic-private-token'});assert.equal((await store.get()).refresh_token,'synthetic-private-token');
 assert.equal((await fs.readFile(store.file)).includes(Buffer.from('synthetic-private-token')),false);await store.delete();assert.equal(await store.get(),null);
 await assert.rejects(new ProtectedStore(directory,safe,'linux').put({token:'never'}),/Unavailable/);
 safe.isEncryptionAvailable=()=>false;await assert.rejects(store.put({token:'never'}),/Unavailable/);
});
