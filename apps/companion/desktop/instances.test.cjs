'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os');
const {Instances,NativeServices}=require('./instances.cjs');const {fixture}=require('./instances-fixture.cjs');
async function setup(t){const raw=await fs.mkdtemp(path.join(os.tmpdir(),'daemons-instances-test-'));const root=await fs.realpath(raw);t.after(()=>fs.rm(root,{recursive:true,force:true}));const f=await fixture(root);const instance=new Instances({...f,environment:{}});await instance.load();return {...f,instance,root};}
async function snapshot(root) {const rows=[];async function walk(p){for(const e of await fs.readdir(p,{withFileTypes:true})){const f=path.join(p,e.name);if(e.isDirectory())await walk(f);else if(e.isFile())rows.push([path.relative(root,f),(await fs.readFile(f)).toString('base64')]);}}await walk(root);return rows.sort((a,b)=>a[0].localeCompare(b[0]));}
test('discovers distinct hosts, optional daemon, shared memory; read only and no secrets',async t=>{
 const f=await setup(t),before=await snapshot(f.home);const result=await f.instance.refresh();assert.equal(result.instances.length,4);assert.equal(result.instances.filter(x=>x.daemon).length,2);
 const attached=result.instances.filter(x=>x.daemon);assert.equal(attached[0].daemon.id,attached[1].daemon.id);assert.equal(attached[0].daemon.sharedWith.length,1);
 assert(!JSON.stringify(result).includes('never-export-this-token'));assert(!JSON.stringify(result).includes('/synthetic/'));assert.deepEqual(await snapshot(f.home),before);assert.deepEqual(f.services.calls,[]);
 assert.equal(result.instances.find(x=>x.host==='openclaw').state,'stopped');assert(result.instances.every(x=>!x.managed));
});
test('explicit management persists; cancellation, stop and restart affect only chosen gateway',async t=>{
 const f=await setup(t);const before=await snapshot(f.home);const id=(await f.instance.refresh()).instances.find(x=>x.profile===f.hermes).id;
 await assert.rejects(f.instance.change(id,'stop',async()=>true),/Connect this instance/);
 await f.instance.change(id,'connect',async()=>false);assert.equal(f.instance.records.length,0);
 await f.instance.change(id,'connect',async()=>true);await f.instance.change(id,'stop',async()=>true);await f.instance.change(id,'restart',async()=>true);
 assert.deepEqual(f.services.calls,[{label:'ai.hermes.gateway',action:'stop'},{label:'ai.hermes.gateway',action:'restart'}]);
 const reopened=new Instances({...f,environment:{}});await reopened.load();assert((await reopened.refresh()).instances.find(x=>x.id===id).managed);
 await reopened.change(id,'forget',async()=>true);assert(!(await reopened.refresh()).instances.find(x=>x.id===id).managed);assert.deepEqual(await snapshot(f.home),before);
});
test('ambiguous services, changed definition and stale confirmation refuse actions',async t=>{
 const f=await setup(t);const id=(await f.instance.refresh()).instances.find(x=>x.profile===f.hermes).id;
 await f.instance.change(id,'connect',async()=>true);
 await assert.rejects(f.instance.change(id,'stop',async()=>{f.services.items[0].digest='f'.repeat(64);return true;}),/service changed/);
 await assert.rejects(f.instance.change(id,'stop',async()=>true),/Connect this instance/);
 f.services.items.push({...f.services.items[0],label:'ai.hermes.other-gateway'});
 assert.equal((await f.instance.refresh()).instances.find(x=>x.id===id).serviceProblem,'ambiguous');assert.equal(f.services.calls.length,0);
});
test('manual profile, malformed metadata, oversize, alias and corrupt registry stay bounded',async t=>{
 const f=await setup(t);const outside=path.join(f.root,'custom');await f.write(path.join(outside,'openclaw.json'),'{ plugins: {}, }');
 assert((await f.instance.add(outside)).instances.some(x=>x.profile===outside));
 await f.write(path.join(outside,'openclaw.json'),'{ invalid');assert.equal((await f.instance.refresh()).instances.find(x=>x.profile===outside).problem,'configuration-unreadable');
 await f.write(path.join(outside,'openclaw.json'),'x'.repeat(1024*1024+1));assert.equal((await f.instance.refresh()).instances.find(x=>x.profile===outside).canConnect,false);
 const alias=path.join(f.home,'.hermes/profiles/alias');await fs.symlink(f.hermes,alias,process.platform==='win32'?'junction':'dir');assert.equal((await f.instance.refresh()).instances.filter(x=>x.profile===f.hermes).length,1);
 await f.write(path.join(f.appData,'instances.json'),'bad');await f.instance.load();assert.equal(f.instance.records.length,0);assert(f.instance.registryWarning);
});
test('IPC action enum never becomes a command and unknown service status refuses control',async t=>{
 const f=await setup(t);const id=(await f.instance.refresh()).instances[0].id;
 await assert.rejects(f.instance.change(id,'rm -rf',async()=>true),/Unsupported instance action/);
 await f.instance.change(id,'connect',async()=>true);f.services.items[0].state='unknown';await assert.rejects(f.instance.change(id,'stop',async()=>true),/Connect this instance/);
 assert.equal(f.services.calls.length,0);await assert.rejects(f.instance.target(id,'secret'));
});
test('Windows enumerates owned root and DaemonHarness tasks and controls exact folder',async()=>{
 const calls=[];const item={label:'com.daemonharness.test.abc.hermes-gateway',folder:'\\DaemonHarness',xml:'<synthetic/>',executable:'C:\\python.exe',arguments:'synthetic',cwd:'C:\\instance',state:4};
 const s=new NativeServices({home:'C:\\home',platform:'win32',run:async(exe,args,options)=>{calls.push({exe,args,options});return {stdout:JSON.stringify([item])};}});
 const list=await s.list();assert.equal(list.items.length,1);assert.equal(list.items[0].state,'running');assert(calls[0].args.at(-1).includes('\\DaemonHarness'));assert(calls[0].args.at(-1).includes('Principal.RunLevel -ne 0'));assert(calls[0].args.at(-1).includes('NTAccount]::new($owner)'));assert(calls[0].args.at(-1).includes('Translate([System.Security.Principal.SecurityIdentifier]).Value'));assert(calls[0].args.at(-1).includes('$owner -ne $sid'));assert(calls[0].args.at(-1).includes('UTF8Encoding'));assert(calls[0].args.at(-1).includes('$actions.Item(1)'));assert(!Object.keys(calls[0].options.env).some(x=>x.toLowerCase()==='psmodulepath'));
 await s.control(list.items[0],'stop');assert(calls[1].args.at(-1).includes("GetFolder('\\DaemonHarness')"));assert(calls[1].args.at(-1).includes('$t.Stop(0)'));assert(!calls[1].args.at(-1).includes('.Run('));
 await assert.rejects(s.control({...list.items[0],folder:"\\';danger"},'start'));
});

test('failed settings write cannot activate a service grant; manual registration can be forgotten',async t=>{
 const f=await setup(t);const id=(await f.instance.refresh()).instances.find(r=>r.profile===f.hermes).id;
 const save=f.instance.save;f.instance.save=async()=>{throw new Error('Synthetic disk refusal');};
 await assert.rejects(f.instance.change(id,'connect',async()=>true),/disk refusal/);
 assert(!(await f.instance.refresh()).instances.find(r=>r.id===id).managed);assert.equal(f.instance.records.length,0);
 f.instance.save=save;const custom=path.join(f.root,'manual');await f.write(path.join(custom,'openclaw.json'),'{}');
 const added=(await f.instance.add(custom)).instances.find(r=>r.profile===custom);assert(added.registered);assert(!added.connected);
 await f.instance.change(added.id,'forget',async()=>true);assert(!(await f.instance.refresh()).instances.some(r=>r.id===added.id));
 assert.equal(await fs.readFile(path.join(custom,'openclaw.json'),'utf8'),'{}');assert.equal(f.services.calls.length,0);
});

test('new Companion instances remain discoverable alongside the legacy installation',async t=>{
 const f=await setup(t);const {SetupHomes}=require('./setup-homes.cjs');const homes=new SetupHomes(path.join(f.base,'Companion'));
 const before=await snapshot(f.home);const profiles=[];
 for(let i=0;i<2;i++) {const root=await homes.create();const profile=path.join(root,'hosts/hermes/installations','c'.repeat(31)+i,'profile');await f.write(path.join(profile,'config.yaml'),'model: synthetic\n');profiles.push(profile);}
 const rows=(await f.instance.refresh()).instances;assert.equal(rows.length,6);assert(profiles.every(p=>rows.some(r=>r.profile===p)));
 const after=await snapshot(f.home);for(const row of before)assert(after.some(x=>JSON.stringify(x)===JSON.stringify(row)));
 assert.equal(f.services.calls.length,0);
});

test('daemon settings are bound to reviewed paths, shared hosts and an opaque form token',async t=>{
 const f=await setup(t);const id=(await f.instance.refresh()).instances.find(r=>r.profile===f.hermes).id;
 const calls=[];const values={name:'Nova',gender:'feminine',preferred_name:'',pronouns:''};
 const run=async request=>{calls.push(request);return {revision:'r1',values,canEditName:true,canEditUser:false};};
 const d=await f.instance.inspectDaemon(id,run);
 assert.equal(d.shared.length,1);assert.equal(calls[0].root,path.join(f.memory,'.daemon'));assert(d.token);assert.equal(calls[0].action,'inspect');
 assert.deepEqual(await f.instance.saveDaemon(id,d.token,values,run,async()=>false),{cancelled:true});assert.equal(calls.length,1);
 await assert.rejects(f.instance.saveDaemon(id,'stale',values,run,async()=>true),/changed/);
 await assert.rejects(f.instance.saveDaemon(id,d.token,{...values,root:'/untrusted'},run,async()=>true),/Check the names/);
 const saved=await f.instance.saveDaemon(id,d.token,values,run,async()=>true);
 assert.notEqual(saved.token,d.token);assert.equal(calls.at(-1).action,'save');assert.equal(calls.at(-1).revision,'r1');assert.equal(calls.at(-1).root,path.join(f.memory,'.daemon'));
 assert.equal(f.services.calls.length,0);assert.equal(f.instance.records.length,0);
});
test('changing a memory pointer during shared confirmation refuses the save',async t=>{
 const f=await setup(t);const id=(await f.instance.refresh()).instances.find(r=>r.profile===f.hermes).id;
 let writes=0;const values={name:'Nova',gender:'feminine',preferred_name:'',pronouns:''};
 const run=async request=>{if(request.action==='save')writes++;return {revision:'r1',values};};
 const d=await f.instance.inspectDaemon(id,run);
 const other=path.join(f.home,'other-memory');await fs.mkdir(other);
 await assert.rejects(f.instance.saveDaemon(id,d.token,values,run,async()=>{await f.write(path.join(f.hermes,'.env'),'DAEMON_CORE_MEMORY_ROOT="'+other+'"\n');return true;}),/changed/);
 assert.equal(writes,0);assert.equal(f.services.calls.length,0);
});
test('legacy identity location and a single explicit account are detected without leaking tokens',async t=>{
 const f=await setup(t);const legacy=path.join(f.home,'legacy-memory');
 await f.write(path.join(legacy,'core/daemon_identity.json'),JSON.stringify({name:'Legacy',gender:'feminine'}));
 await f.write(path.join(f.hermes,'.env'),'DAEMON_CORE_MEMORY_ROOT="'+legacy+'"\nTELEGRAM_ALLOWED_USERS=12345\nTELEGRAM_BOT_TOKEN=never-export-this-token\n');
 const r=(await f.instance.refresh()).instances.find(r=>r.profile===f.hermes);assert.equal(r.daemon.name,'Legacy');assert.equal(r.daemon.runtime,legacy);
 let request;await f.instance.inspectDaemon(r.id,async x=>{request=x;return {revision:'r1',values:{}};});
 assert.deepEqual(request.binding,{user_id:'12345',platform:'telegram'});assert(!JSON.stringify(request).includes('never-export-this-token'));
 await f.write(path.join(f.hermes,'.env'),'DAEMON_CORE_MEMORY_ROOT="'+legacy+'"\nTELEGRAM_ALLOWED_USERS=12345,67890\n');
 await f.instance.inspectDaemon(r.id,async x=>{request=x;return {revision:'r1',values:{}};});assert.equal(request.binding,null);
});

test('account inventory distinguishes detected accounts from the saved preference binding',async t=>{
 const f=await setup(t);await f.write(path.join(f.hermes,'.env'),'DAEMON_CORE_MEMORY_ROOT="'+path.join(f.memory,'.daemon')+'"\nTELEGRAM_ALLOWED_USERS=12345\n');
 const cfg=JSON.parse(await fs.readFile(path.join(f.openclaw,'openclaw.json'),'utf8'));cfg.plugins.entries['daemon-harness'].config.userId='12345';await f.write(path.join(f.openclaw,'openclaw.json'),JSON.stringify(cfg));
 const before=await snapshot(f.home),id=(await f.instance.refresh()).instances.find(r=>r.profile===f.hermes).id;
 const values={name:'Aster',gender:'feminine',preferred_name:'Morgan',pronouns:'they/them'},scope={user_id:'12345',platform:'telegram'};
 const d=await f.instance.inspectDaemon(id,async()=>({revision:'r1',values,userScope:scope}));
 assert.equal(d.accounts.length,2);assert.equal(d.accounts.filter(a=>a.addressing).length,1);
 assert(d.accounts.find(a=>a.platform==='telegram').addressing);assert(!d.accounts.find(a=>a.platform==='openclaw').addressing);
 assert.deepEqual(d.accounts.find(a=>a.platform==='openclaw').hosts,['openclaw']);
 const savedOnly=await f.instance.inspectDaemon(id,async()=>({revision:'r1',values,userScope:{platform:'telegram',user_id:'67890'}}));
 assert.equal(savedOnly.accounts.length,3);assert.deepEqual(savedOnly.accounts.find(a=>a.addressing).hosts,[]);
 assert.deepEqual(await snapshot(f.home),before);assert.equal(f.services.calls.length,0);
 // A detected sibling account changed since review: do not save against stale scope.
 const cfg2={...cfg,plugins:{...cfg.plugins,entries:{'daemon-harness':{...cfg.plugins.entries['daemon-harness'],config:{...cfg.plugins.entries['daemon-harness'].config,userId:'different'}}}}};
 await f.write(path.join(f.openclaw,'openclaw.json'),JSON.stringify(cfg2));let writes=0;
 await assert.rejects(f.instance.saveDaemon(id,savedOnly.token,values,async()=>{writes++;},async()=>true),/changed/);assert.equal(writes,0);
});

for(const platform of ['darwin','win32'])test(`uninstall confirms the installation name on ${platform}; original daemon name stays internal`,async t=>{
 const f=await setup(t);f.instance.platform=platform;
 const profile=path.join(f.base,'Companion/instance-ABC123/hosts/hermes/installations','a'.repeat(32),'profile');
 await f.write(path.join(profile,'config.yaml'),'model: synthetic\n');
 const id=(await f.instance.refresh()).instances.find(r=>r.profile===profile).id;
 let mutations=0;
 const run=async request=>{assert.equal(request.profile,profile);assert.equal(request.base,f.base);if(request.operation==='apply'){assert.equal(request.name,'Nova');mutations++;}return {revision:'r1',profile,memory:f.memory,name:'Nova',action:request.action,done:request.operation==='apply'};};
 const plan=await f.instance.reviewRemoval(id,'uninstall',run);
 assert.equal(plan.installationName,'Hermes - Nova');assert.equal(plan.confirmationText,plan.installationName);
 for(const wrong of ['nova','Nova','Hermes'])await assert.rejects(f.instance.applyRemoval(id,plan.token,wrong,run),/changed/);
 assert.equal(mutations,0);
 await assert.rejects(f.instance.applyRemoval(id,'wrong',plan.confirmationText,run),/changed/);assert.equal(mutations,0);
 const done=await f.instance.applyRemoval(id,plan.token,plan.confirmationText,run);assert(done.done);assert.equal(done.installationName,plan.installationName);assert.equal(mutations,1);
 await assert.rejects(f.instance.applyRemoval(id,plan.token,plan.confirmationText,run),/changed/);
 assert.equal(await f.instance.removalTarget(id,plan.token,'memory'),f.memory);
 await assert.rejects(f.instance.removalTarget(id,plan.token,'/arbitrary'),/unsafe/);
 const detach=await f.instance.reviewRemoval(id,'detach',run);assert.equal(detach.confirmationText,'Nova');
 await assert.rejects(f.instance.applyRemoval(id,detach.token,plan.confirmationText,run),/changed/);assert.equal(mutations,1);
 assert((await f.instance.applyRemoval(id,detach.token,'Nova',run)).done);assert.equal(mutations,2);
});
test('the memory goes to the trash only after a finished uninstall that offered it, and only once',async t=>{
 const f=await setup(t);f.instance.platform='darwin';
 const profile=path.join(f.base,'Companion/instance-ABC123/hosts/hermes/installations','a'.repeat(32),'profile');
 await f.write(path.join(profile,'config.yaml'),'model: synthetic\n');
 const id=(await f.instance.refresh()).instances.find(r=>r.profile===profile).id;
 const memory=path.join(f.base,'Companion/instance-ABC123/data/memory'),checks=[];await f.write(path.join(memory,'note.md'),'synthetic');
 let vault=memory;
 const run=async request=>{if(request.operation==='memory-check'){checks.push(request);return {memory:vault,revision:'r1'};}
   return {revision:'r1',profile,memory:vault,name:'Nova',action:request.action,done:request.operation==='apply',memoryDeletable:request.action==='uninstall'};};
 // The vault the other listed hosts use is never released.
 vault=f.memory;const shared=await f.instance.reviewRemoval(id,'uninstall',run);await f.instance.applyRemoval(id,shared.token,shared.confirmationText,run);
 await assert.rejects(f.instance.memoryRemoval(id,shared.token,run),/shared/);assert.equal(checks.length,0);vault=memory;
 const plan=await f.instance.reviewRemoval(id,'uninstall',run);
 await assert.rejects(f.instance.memoryRemoval(id,plan.token,run),/changed/);
 await f.instance.applyRemoval(id,plan.token,plan.confirmationText,run);
 await assert.rejects(f.instance.memoryRemoval(id,'wrong',run),/changed/);assert.equal(checks.length,0);
 assert.equal(await f.instance.memoryRemoval(id,plan.token,run),memory);
 assert.deepEqual(checks.map(c=>[c.operation,c.revision,c.profile]),[['memory-check','r1',profile]]);
 await assert.rejects(f.instance.memoryRemoval(id,plan.token,run),/changed/);
 // A detach never offers it, and a check naming another folder is refused.
 const detach=await f.instance.reviewRemoval(id,'detach',run);await f.instance.applyRemoval(id,detach.token,'Nova',run);
 await assert.rejects(f.instance.memoryRemoval(id,detach.token,run),/changed/);
 const again=await f.instance.reviewRemoval(id,'uninstall',run);await f.instance.applyRemoval(id,again.token,again.confirmationText,run);
 await assert.rejects(f.instance.memoryRemoval(id,again.token,async()=>({memory:path.dirname(memory),revision:'r1'})),/unsafe/);
});

test('removal stays unavailable on external hosts; uninstalled tombstone is excluded',async t=>{
 const f=await setup(t);const rows=(await f.instance.refresh()).instances;
 const id=rows.find(r=>r.profile===f.hermes).id;
 await assert.rejects(f.instance.reviewRemoval(id,'detach',async()=>{}),/ownership/);
 f.instance.platform='win32';await assert.rejects(f.instance.reviewRemoval(id,'detach',async()=>{}),/ownership/);
 f.instance.platform='darwin';
 const attempt=path.join(f.base,'Companion/instance-ABC123/hosts/hermes/installations','a'.repeat(32));const profile=path.join(attempt,'profile');
 await f.write(path.join(profile,'config.yaml'),'model: synthetic\n');
 assert((await f.instance.refresh()).instances.some(r=>r.profile===profile));
 await f.write(path.join(attempt,'companion-removal.json'),JSON.stringify({schema:'daemon.companion-removal.v1',profile,status:'done',action:'uninstall'}));
 assert(!(await f.instance.refresh()).instances.some(r=>r.profile===profile));
 assert((await f.instance.refresh()).instances.some(r=>r.profile===f.hermes));
});


test('health resolves private state receipts and service overrides without exposing technical paths',async t=>{
 const f=await setup(t),state=path.join(f.root,'state');
 await f.write(path.join(state,'receipts/example.jsonl'),'');
 await fs.appendFile(path.join(f.hermes,'.env'),'\nDAEMON_CORE_STATE_ROOT='+state+'\n');
 const inventory=await f.instance.refresh(),row=[...f.instance.rows.values()].find(r=>r.profile===f.hermes);
 assert.equal(row.healthPaths.receipts,path.join(state,'receipts'));
 assert(!JSON.stringify(inventory).includes(state));
 f.services.items[0].env.DAEMON_CORE_CONTINUITY_RECEIPT_DIR=path.join(f.root,'explicit');
 await f.instance.refresh();assert.equal(f.instance.rows.get(row.id).healthPaths.receipts,path.join(f.root,'explicit'));
});

// The private runtime writes each value with json.dumps (openclaw/first_profile.py):
// on Windows every backslash of a path is escaped.
for(const [writer,quote] of [['quoted',v=>`"${v}"`],['json.dumps',v=>JSON.stringify(v)]])test(`associated OpenClaw shares its daemon card without an active bridge (${writer} .env)`,async t=>{
 const f=await setup(t),state=path.join(f.root,'state'),source=path.join(f.root,'release/src');
 await fs.mkdir(source,{recursive:true});
 const ref=require('./host-associations.cjs').reference(f.openclaw);
 await f.write(path.join(state,'host-bindings',ref+'.json'),JSON.stringify({schema:'daemon.host-execution.v1',host_ref:ref,status:'active',host:'openclaw',user_id:'12345',profile:f.openclaw,memory:path.join(f.memory,'.daemon'),executables:[]}));
 const cfg=JSON.parse(await fs.readFile(path.join(f.openclaw,'openclaw.json'),'utf8'));cfg.plugins.entries['daemon-harness'].config.userId='12345';await f.write(path.join(f.openclaw,'openclaw.json'),JSON.stringify(cfg));
 await f.write(path.join(f.openclaw,'.env'),Object.entries({DAEMON_CORE_HOST_REF:ref,DAEMON_CORE_STATE_ROOT:state,DAEMON_CORE_SRC:source,DAEMON_CORE_MEMORY_ROOT:path.join(f.memory,'.daemon')}).map(([k,v])=>`${k}=${quote(v)}`).join('\n'));
 f.services.items=f.services.items.filter(s=>!s.label.includes('openclaw-api'));
 const before=await snapshot(f.root),rows=(await f.instance.refresh()).instances;
 const h=rows.find(r=>r.host==='hermes'&&r.daemon),o=rows.find(r=>r.profile===f.openclaw);
 assert.equal(o.daemon.id,h.daemon.id);assert.equal(o.daemon.name,'Aster');assert.equal(o.problem,'');assert.equal(o.state,'stopped');assert.deepEqual(await snapshot(f.root),before);
 await fs.unlink(path.join(state,'host-bindings',ref+'.json'));
 const invalid=(await f.instance.refresh()).instances.find(r=>r.profile===f.openclaw);assert.equal(invalid.problem,'association-unreadable');assert.equal(invalid.daemon.memory,null);
});
for(const platform of ['darwin','win32'])test(`uninstall refuses an undiscovered dependent host on ${platform} before requesting backend mutation`,async t=>{
 const f=await setup(t);f.instance.platform=platform;
 const profile=path.join(f.base,'Companion/instance-ABC123/hosts/hermes/installations','a'.repeat(32),'profile');
 const state=path.join(path.dirname(profile),'daemon/data/state'),destination=path.join(f.root,'unavailable-openclaw');
 const ref=require('./host-associations.cjs').reference(destination);
 await f.write(path.join(profile,'config.yaml'),'model: synthetic\n');
 await f.write(path.join(profile,'.env'),`DAEMON_CORE_STATE_ROOT="${state}"\n`);
 await f.write(path.join(state,'host-bindings',ref+'.json'),JSON.stringify({schema:'daemon.host-execution.v1',host_ref:ref,status:'active',host:'openclaw',user_id:'12345',profile:destination,memory:f.memory,executables:[]}));
 const id=(await f.instance.refresh()).instances.find(r=>r.profile===profile).id;
 let invoked=false;await assert.rejects(f.instance.reviewRemoval(id,'uninstall',async()=>{invoked=true;}),error=>/removal.shared/.test(error.message)&&error.removalDiagnostic?.stage==='host_registration');assert.equal(invoked,false);
});
for(const platform of ['darwin','win32'])test(`an unreadable host registry is left to the removal's own task scan on ${platform}`,async t=>{
 const f=await setup(t);f.instance.platform=platform;
 const profile=path.join(f.base,'Companion/instance-ABC123/hosts/hermes/installations','a'.repeat(32),'profile');
 const state=path.join(path.dirname(profile),'daemon/data/state');
 await f.write(path.join(profile,'config.yaml'),'model: synthetic\n');
 await f.write(path.join(profile,'.env'),`DAEMON_CORE_STATE_ROOT="${state}"\n`);
 await f.write(path.join(state,'host-bindings','c'.repeat(32)+'.json'),'{not json');
 const id=(await f.instance.refresh()).instances.find(r=>r.profile===profile).id;
 let invoked=false;
 await f.instance.reviewRemoval(id,'uninstall',async request=>{invoked=true;return {revision:'r1',profile,memory:f.memory,name:'Nova',action:request.action,done:false};});
 assert.equal(invoked,true);
});
for(const platform of ['darwin','win32'])test(`an OpenClaw added to the daemon is left to the removal itself on ${platform}`,async t=>{
 const f=await setup(t);f.instance.platform=platform;
 const profile=path.join(f.base,'Companion/instance-ABC123/hosts/hermes/installations','a'.repeat(32),'profile');
 const state=path.join(path.dirname(profile),'daemon/data/state');
 const added=path.join(f.base,'Companion/private/hosts/openclaw/installations','b'.repeat(32),'profile');
 const ref=require('./host-associations.cjs').reference(added);
 await f.write(path.join(profile,'config.yaml'),'model: synthetic\n');
 await f.write(path.join(profile,'.env'),`DAEMON_CORE_STATE_ROOT="${state}"\n`);
 await f.write(path.join(state,'host-bindings',ref+'.json'),JSON.stringify({schema:'daemon.host-execution.v1',host_ref:ref,status:'active',host:'openclaw',user_id:'12345',profile:added,memory:f.memory,executables:[]}));
 await f.write(path.join(f.base,'Companion/private/host-setups','b'.repeat(32)+'.json'),JSON.stringify({source:profile,profile:added}));
 const id=(await f.instance.refresh()).instances.find(r=>r.profile===profile).id;
 let invoked=false;
 await f.instance.reviewRemoval(id,'uninstall',async request=>{invoked=true;return {revision:'r1',profile,memory:f.memory,name:'Nova',action:request.action,done:false};});
 assert.equal(invoked,true);
});
