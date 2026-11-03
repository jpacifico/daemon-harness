'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os');
const {resolve,assertNoDependents,reference}=require('./host-associations.cjs');
async function fixture(t) {
 const root=await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(),'daemon-binding-ui-')));
 t.after(()=>fs.rm(root,{recursive:true,force:true}));
 const state=path.join(root,'state'),profile=path.join(root,'openclaw'),memory=path.join(root,'memory/.daemon'),source=path.join(root,'release/src');
 for(const p of [path.join(state,'host-bindings'),profile,memory,source])await fs.mkdir(p,{recursive:true});
 const ref=reference(profile),file=path.join(state,'host-bindings',ref+'.json');
 const data={schema:'daemon.host-execution.v1',host_ref:ref,status:'active',host:'openclaw',user_id:'test-user',memory,profile,executables:[]};
 await fs.writeFile(file,JSON.stringify(data));
 const env={DAEMON_CORE_HOST_REF:ref,DAEMON_CORE_STATE_ROOT:state,DAEMON_CORE_MEMORY_ROOT:memory,DAEMON_CORE_SRC:source};
 return {root,state,profile,memory,source,file,data,env};
}
test('explicit association identifies the common runtime without a running API',async t=>{
 const f=await fixture(t);assert.deepEqual(await resolve(f.profile,f.env,{userId:'test-user'}),{memory:f.memory,source:f.source});
 assert.equal(await resolve(f.profile,{},{}),null);
});
test('missing or mismatched explicit association never falls back to default profiles',async t=>{
 const f=await fixture(t);
 await assert.rejects(resolve(f.profile,f.env,{userId:'other'}));
 await assert.rejects(resolve(f.profile,{...f.env,DAEMON_CORE_MEMORY_ROOT:f.source},{userId:'test-user'}));
 await fs.unlink(f.file);await assert.rejects(resolve(f.profile,f.env,{userId:'test-user'}));
});
test('removal remains blocked when the associated host is missing from discovery',async t=>{
 const f=await fixture(t);await fs.rm(f.profile,{recursive:true});
 await assert.rejects(assertNoDependents(f.state,path.join(f.root,'hermes')),/depends/);
 await fs.unlink(f.file);await assertNoDependents(f.state,path.join(f.root,'hermes'));
});
test('corrupt and aliased registries block removal and binding',async t=>{
 const f=await fixture(t);await fs.writeFile(f.file,'{}');
 await assert.rejects(assertNoDependents(f.state,f.profile));
 await fs.unlink(f.file);const elsewhere=path.join(f.root,'external.json');await fs.writeFile(elsewhere,JSON.stringify(f.data));await fs.symlink(elsewhere,f.file);
 await assert.rejects(resolve(f.profile,f.env,{userId:'test-user'}));await assert.rejects(assertNoDependents(f.state,f.profile));
});
