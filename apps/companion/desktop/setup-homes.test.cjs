'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os');
const {SetupHomes}=require('./setup-homes.cjs');
const {Backend}=require('./backend.cjs');
async function fixture(t) {
  const root=await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(),'companion-homes-')));
  t.after(()=>fs.rm(root,{recursive:true,force:true}));
  const homes=new SetupHomes(path.join(root,'Companion'));
  return {root,homes};
}
test('legacy resume and successive new installations never replace previous data',async t=>{
  const {root,homes}=await fixture(t);
  const legacy=await homes.current();
  assert.equal(legacy,path.join(root,'Companion/private'));
  await fs.mkdir(legacy,{recursive:true});await fs.writeFile(path.join(legacy,'receipt'),'legacy exact');
  const first=await homes.create();await fs.writeFile(path.join(first,'receipt'),'first exact');
  const second=await homes.create();assert.notEqual(first,second);assert.notEqual(second,legacy);
  assert.equal(await new SetupHomes(homes.root).current(),second);
  assert.equal(await fs.readFile(path.join(first,'receipt'),'utf8'),'first exact');
  assert.equal(await fs.readFile(path.join(legacy,'receipt'),'utf8'),'legacy exact');
});
test('corrupt, missing and escaping selections block resume; explicit new installation can recover',async t=>{
  const {homes}=await fixture(t);await homes.create();
  for(const data of ['{','null',JSON.stringify({schema:1,directory:'../outside'}),JSON.stringify({schema:1,directory:'instance-AAAAAA'})]) {
    await fs.writeFile(homes.file,data);await assert.rejects(homes.current());
  }
  const fresh=await homes.create();assert.equal(await homes.current(),fresh);
});
test('symlinked directories and invalid pointer destinations are refused without writes outside the owned root',async t=>{
  const {root,homes}=await fixture(t);const first=await homes.create();
  const outside=path.join(root,'outside');await fs.mkdir(outside);
  await fs.rmdir(first);await fs.symlink(outside,first,process.platform==='win32'?'junction':'dir');
  await assert.rejects(homes.current(),/must be direct/);
  const redirected=new SetupHomes(path.join(root,'redirected'));
  await fs.symlink(outside,redirected.root,process.platform==='win32'?'junction':'dir');
  await assert.rejects(redirected.create(),/must be direct/);assert.deepEqual(await fs.readdir(outside),[]);
  // Directory selection instead of a regular pointer: failed publication leaves no new root.
  await fs.unlink(homes.file);await fs.mkdir(homes.file);const before=await fs.readdir(homes.root);
  await assert.rejects(homes.create());assert.deepEqual(await fs.readdir(homes.root),before);
});
test('backend serializes new setup, rejects concurrent actions and resumes exact selection',async t=>{
  const {homes}=await fixture(t);const events=[];let release;
  const b=new Backend({setupHomes:homes,emit:e=>events.push(e)});
  b.prepare=async()=>{await new Promise(resolve=>release=resolve);throw new Error('Synthetic stop before launching Python.');};
  await assert.rejects(b.start('arbitrary'));assert.equal(b.active,false);
  await b.start('new');await assert.rejects(b.start('new'),/already running/);
  while(!release)await new Promise(r=>setTimeout(r,5));
  const first=b.installHome;release();await b.stopped;
  const current=await homes.current();assert.equal(first,current);
  release=null;await b.start();while(!release)await new Promise(r=>setTimeout(r,5));assert.equal(b.installHome,first);release();await b.stopped;
  assert.equal(events.at(-1).type,'idle');assert.equal((await fs.readdir(homes.root)).filter(s=>s.startsWith('instance-')).length,1);
});

test('startup hint is read-only and distinguishes fresh, saved, legacy and unreadable setups',async t=>{
  const {root,homes}=await fixture(t);
  assert.equal(await homes.summary(),'none');
  assert.deepEqual(await fs.readdir(root),[],'First launch created setup files');
  const legacy=await homes.current();await fs.mkdir(legacy,{recursive:true});
  assert.equal(await homes.summary(),'none','An empty bootstrap directory is not an existing setup');
  await fs.writeFile(path.join(legacy,'retained'),'exact legacy data');
  assert.equal(await homes.summary(),'saved');
  const selected=await homes.create();
  const before=await fs.readFile(homes.file,'utf8');
  assert.equal(await new SetupHomes(homes.root).summary(),'saved');
  assert.equal(await fs.readFile(homes.file,'utf8'),before);
  assert.deepEqual(await fs.readdir(selected),[]);
  await fs.writeFile(homes.file,'{');
  assert.equal(await homes.summary(),'unreadable');
  assert.equal(await fs.readFile(homes.file,'utf8'),'{');
  assert.equal(await fs.readFile(path.join(legacy,'retained'),'utf8'),'exact legacy data');
});

test('completed setup is distinguished from retained downloads and from another selected setup',async t=>{
  const {homes}=await fixture(t);
  const first=await homes.create();
  const {installedSetup}=require('./setup-homes-fixture.cjs');
  const proof=await installedSetup(first);
  const before=await Promise.all(Object.values(proof.files).map(file=>fs.readFile(file,'utf8')));
  assert.equal(await homes.summary(),'installed','A completed daemon must not offer Resume setup');
  assert.deepEqual(await Promise.all(Object.values(proof.files).map(file=>fs.readFile(file,'utf8'))),before);
  await homes.create();
  assert.equal(await homes.summary(),'saved','Another completed installation must not complete the selected one');
  await fs.unlink(homes.file);
  await installedSetup(await homes.current());
  assert.equal(await homes.summary(),'installed','Legacy completed installations are recognized too');
});

test('missing completion receipt remains resumable; malformed or redirected evidence cannot claim completion',async t=>{
  const {root,homes}=await fixture(t);const selected=await homes.create();
  const {installedSetup}=require('./setup-homes-fixture.cjs');
  const proof=await installedSetup(selected);
  await fs.unlink(proof.files.receipt);
  assert.equal(await homes.summary(),'saved');
  for(const content of ['{','null',JSON.stringify({...proof.receipt,schema:'wrong'}),' '.repeat(8193)]) {
    await fs.writeFile(proof.files.receipt,content);
    assert.equal(await homes.summary(),'unreadable');
  }
  await installedSetup(selected);
  await fs.writeFile(proof.files.owner,JSON.stringify({...proof.host,installation_id:'e'.repeat(32)}));
  assert.equal(await homes.summary(),'unreadable');
  await installedSetup(selected);
  await fs.writeFile(proof.files.pointer,JSON.stringify({...proof.host,installation_id:'../../outside'}));
  assert.equal(await homes.summary(),'unreadable');
  await installedSetup(selected);
  const outside=path.join(root,'outside');await fs.mkdir(outside);
  await fs.rm(path.join(proof.attempt,'daemon'),{recursive:true});
  await fs.symlink(outside,path.join(proof.attempt,'daemon'),process.platform==='win32'?'junction':'dir');
  assert.equal(await homes.summary(),'unreadable');
  assert.deepEqual(await fs.readdir(outside),[]);
});

test('owned attempts resume by ID independently, reject completed or malformed receipts and never select on read',async t=>{
 const {homes}=await fixture(t);const first=await homes.create(),second=await homes.create();
 const before=await fs.readFile(homes.file,'utf8');const listed=await homes.list();assert.equal(listed.length,2);
 const firstId=listed.find(x=>homes.roots.get(x.id)===first).id;
 assert.equal(await homes.resolve(firstId),first);assert.equal(await fs.readFile(homes.file,'utf8'),before);
 assert.equal(await homes.select(firstId),first);assert.equal(await homes.current(),first);
 await assert.rejects(homes.select('../outside'));assert.equal(await homes.current(),first);
 const proof=await require('./setup-homes-fixture.cjs').installedSetup(first);
 await assert.rejects(homes.select(firstId),/cannot be resumed/);
 await fs.writeFile(proof.files.receipt,'{');await assert.rejects(homes.select(firstId),/cannot be resumed/);
 assert.equal((await homes.list()).find(x=>x.id===firstId).status,'unreadable');
 const legacy=path.join(homes.root,'private');await fs.mkdir(legacy);await fs.writeFile(path.join(legacy,'retained'),'exact');
 const legacyId=(await homes.list()).find(x=>homes.roots.get(x.id)===legacy).id;
 await homes.select(legacyId);assert.equal(await homes.current(),legacy);
 assert.equal(await fs.readFile(path.join(legacy,'retained'),'utf8'),'exact');
 assert.notEqual(second,first);
});
test('setup inventory ignores redirected siblings and binds reports to the exact resumed root',async t=>{
 const {root,homes}=await fixture(t);const first=await homes.create();const second=await homes.create();
 const other=path.join(root,'outside');await fs.mkdir(other);await fs.symlink(other,path.join(homes.root,'instance-AAAAAA'),process.platform==='win32'?'junction':'dir');
 const rows=await homes.list();assert.equal(rows.length,2);
 const firstId=rows.find(x=>homes.roots.get(x.id)===first).id,secondId=rows.find(x=>homes.roots.get(x.id)===second).id;
 const b=new Backend({setupHomes:homes,emit:()=>{},metadata:{revision:'a'.repeat(40)}});
 b.prepare=async()=>{b.event({type:'phase',name:b.installHome===first?'model':'telegram'});throw new Error('synthetic');};
 await b.start('setup',firstId);await b.stopped;const {Diagnostics}=require('./diagnostics.cjs');
 const firstReport=new Diagnostics(path.join(first,'diagnostics')).read();assert.equal(firstReport.events.at(-1).phase,'model');
 await b.start('setup',secondId);await b.stopped;
 assert.equal(new Diagnostics(path.join(second,'diagnostics')).read().events.at(-1).phase,'telegram');
 assert.deepEqual(new Diagnostics(path.join(first,'diagnostics')).read(),firstReport);
 assert.equal(await homes.current(),second);assert.deepEqual(await fs.readdir(other),[]);
 assert(!JSON.stringify(firstReport).includes(first));
});

test('discard markers hide completed cleanup and prohibit partial cleanup resume',async()=>{
  const root=await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(),'setup-discard-test-')));
  try {
    const homes=new SetupHomes(root),folder=await homes.create();
    const id=(await homes.list())[0].id;
    await fs.writeFile(path.join(folder,'setup-discard.json'),JSON.stringify({schema:'daemon.companion-setup-discard.v1',status:'failed'}));
    assert.equal(await homes.summary(folder),'discarding');
    await assert.rejects(homes.select(id));
    await fs.writeFile(path.join(folder,'setup-discard.json'),JSON.stringify({schema:'daemon.companion-setup-discard.v1',status:'done'}));
    assert.equal(await homes.summary(),'none');assert.deepEqual(await homes.list(),[]);
    assert.equal(await new SetupHomes(root).summary(),'none');
    const fresh=await homes.create();assert.notEqual(fresh,folder);
  }finally{await fs.rm(root,{recursive:true,force:true});}
});

test('first OpenClaw setup is resumable, named correctly and complete without Hermes',async t=>{
  const {homes}=await fixture(t),directory=await homes.create(),id='d'.repeat(32);
  const root=path.join(directory,'hosts/openclaw'),attempt=path.join(root,'installations',id);
  const proof={schema:'daemon.openclaw-first.v1',installation_id:id,target_os:'macos',acquisition:'a'.repeat(64),api_port:12345,gateway_port:12346};
  await fs.mkdir(path.join(attempt,'profile'),{recursive:true});await fs.mkdir(path.join(attempt,'daemon'));
  await fs.writeFile(path.join(root,'current.json'),JSON.stringify(proof));
  assert.equal(await homes.summary(directory),'saved');
  const [row]=await homes.list();assert.equal(row.host,'openclaw');assert.equal(row.profile,path.join(attempt,'profile'));
  await homes.select(row.id);
  await fs.writeFile(path.join(attempt,'owner.json'),JSON.stringify({status:'installed',installation_id:id,revision:proof.acquisition}));
  await fs.writeFile(path.join(attempt,'daemon/.daemon-harness-installation.json'),JSON.stringify({schema:'daemon.companion-installation-scope.v1',host:proof}));
  await fs.writeFile(path.join(attempt,'daemon-setup.json'),JSON.stringify({schema:'daemon.private-setup.v1',username:'Synthetic_Bot',user_id:'777001'}));
  assert.equal(await homes.summary(directory),'installed');
  await assert.rejects(homes.select(row.id),/cannot be resumed/);
  // Its removal by the app hides the setup like a removed Hermes daemon; only
  // a record bound to this profile counts, and a rolled-back removal keeps it.
  const removal=status=>fs.writeFile(path.join(attempt,'companion-removal.json'),
    JSON.stringify({schema:'daemon.companion-removal.v1',profile:path.join(attempt,'profile'),status,action:'uninstall'}));
  await removal('running');assert.equal(await homes.summary(directory),'unreadable');
  await removal('rolled_back');assert.equal(await homes.summary(directory),'installed');
  await fs.writeFile(path.join(attempt,'companion-removal.json'),
    JSON.stringify({schema:'daemon.companion-removal.v1',profile:path.join(directory,'elsewhere'),status:'done',action:'uninstall'}));
  assert.equal(await homes.summary(directory),'installed');
  await removal('done');assert.equal(await homes.summary(directory),'none');assert.deepEqual(await homes.list(),[]);
  await fs.rm(path.join(attempt,'companion-removal.json'));
  await fs.writeFile(path.join(attempt,'daemon/.daemon-harness-installation.json'),'{}');
  assert.equal(await homes.summary(directory),'unreadable');
  await assert.rejects(fs.access(path.join(directory,'hosts/hermes')));
});
