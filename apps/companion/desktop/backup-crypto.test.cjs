'use strict';
const {test}=require('node:test');const assert=require('node:assert/strict');
const fs=require('node:fs/promises');const os=require('node:os');const path=require('node:path');
const {encrypt,decrypt,deriveKey,encryptWithKey,HEADER}=require('./backup-crypto.cjs');
async function fixture(t){const dir=await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(),'daemon-crypto-test-')));t.after(()=>fs.rm(dir,{recursive:true,force:true}));return name=>path.join(dir,name);}
const password='synthetic-long-password';
test('encrypted streamed round trip, randomized ciphertext and no plaintext staging retained',async t=>{
 const p=await fixture(t),data=Buffer.alloc(3*1024*1024,113);await fs.writeFile(p('source'),data);
 await encrypt(p('source'),p('one'),password);await encrypt(p('source'),p('two'),password);
 assert.notDeepEqual(await fs.readFile(p('one')),await fs.readFile(p('two')));
 await decrypt(p('one'),p('restored'),password);assert.deepEqual(await fs.readFile(p('restored')),data);
 assert.deepEqual((await fs.readdir(path.dirname(p('one')))).sort(),['one','restored','source','two']);
});
test('wrong password, header, ciphertext, tag and truncated archive cannot publish plaintext',async t=>{
 const p=await fixture(t);await fs.writeFile(p('source'),'private synthetic memory');await encrypt(p('source'),p('valid'),password);
 await assert.rejects(decrypt(p('valid'),p('restore'),'different-long-password'),/authenticationFailed/);
 const bytes=await fs.readFile(p('valid'));
 for(const [name,index] of [['header',0],['salt',18],['payload',HEADER],['tag',bytes.length-1]]){
  const corrupt=Buffer.from(bytes);corrupt[index]^=1;await fs.writeFile(p(name),corrupt);
  await assert.rejects(decrypt(p(name),p('restore'),password));await assert.rejects(fs.stat(p('restore')));
 }
 await fs.writeFile(p('truncated'),bytes.subarray(0,bytes.length-4));await assert.rejects(decrypt(p('truncated'),p('restore'),password));
 assert.equal((await fs.readdir(path.dirname(p('one')))).some(n=>n.startsWith('.daemons-')),false);
});
test('existing destination, symlink, short password and cancellation are safe',async t=>{
 const p=await fixture(t);await fs.writeFile(p('source'),'original');await fs.writeFile(p('destination'),'preserved');
 await assert.rejects(encrypt(p('source'),p('destination'),password));assert.equal(await fs.readFile(p('destination'),'utf8'),'preserved');
 if(process.platform!=='win32'){await fs.symlink(p('source'),p('link'));await assert.rejects(encrypt(p('link'),p('new'),password),/unsafeFile/);}
 await assert.rejects(encrypt(p('source'),p('new'),'short'),/passwordLength/);
 const c=new AbortController();c.abort();await assert.rejects(encrypt(p('source'),p('new'),password,{signal:c.signal}),/cancelled/);
 await assert.rejects(fs.stat(p('new')));
});
test('empty file authenticates correctly',async t=>{const p=await fixture(t);await fs.writeFile(p('source'),'');await encrypt(p('source'),p('encrypted'),password);await decrypt(p('encrypted'),p('restored'),password);assert.equal((await fs.stat(p('restored'))).size,0);});
test('a stored key writes files that open with the password alone',async t=>{
 const p=await fixture(t);await fs.writeFile(p('source'),'automatic synthetic memory');
 const material=await deriveKey(password);
 await encryptWithKey(p('source'),p('one'),material);await encryptWithKey(p('source'),p('two'),material);
 assert.notDeepEqual(await fs.readFile(p('one')),await fs.readFile(p('two')));
 await decrypt(p('two'),p('restored'),password);assert.equal(await fs.readFile(p('restored'),'utf8'),'automatic synthetic memory');
 await assert.rejects(decrypt(p('one'),p('other'),'different-long-password'),/authenticationFailed/);
 await assert.rejects(encryptWithKey(p('source'),p('bad'),{salt:'',key:''}),/keyUnavailable/);
});
