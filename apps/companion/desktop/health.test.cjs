'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os'),crypto=require('node:crypto');
const {collect}=require('./health.cjs');const ref=v=>crypto.createHash('sha256').update(v).digest('hex').slice(0,16);
async function fixture(t){const root=await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(),'health-')));t.after(()=>fs.rm(root,{recursive:true,force:true}));const write=async(file,value)=>{await fs.mkdir(path.dirname(file),{recursive:true});await fs.writeFile(file,typeof value==='string'?value:JSON.stringify(value));};const row={host:'hermes',state:'running',daemon:{runtime:path.join(root,'memory'),source:path.join(root,'code/src')},binding:{user_id:'12345',platform:'telegram'},healthPaths:{spool:path.join(root,'spool'),receipts:path.join(root,'receipts')}};await fs.mkdir(row.daemon.runtime);await fs.mkdir(row.healthPaths.spool);return {root,row,write};}
const check=(r,k)=>r.checks.find(c=>c.key===k);
async function receipts(f,status='completed',age=0,extra={}) {const at=new Date(Date.now()-age).toISOString();await f.write(path.join(f.row.healthPaths.receipts,'one.jsonl'),[{schema:'daemon.continuity-receipt.event.v1',turn_id:'test',at,identity:{user_ref:ref('12345'),platform:'telegram',memory_root_ref:ref(f.row.daemon.runtime),source_root_ref:ref(path.dirname(f.row.daemon.source)),...extra}},...['response','cycle'].map(stage=>({schema:'daemon.continuity-receipt.event.v1',turn_id:'test',stage,status,at,details:{secret:'private-content'}}))].map(JSON.stringify).join('\n'));}
test('current evidence, failed passes, pending jobs and no fabricated channel/search success',async t=>{const f=await fixture(t);await receipts(f);await f.write(path.join(f.row.healthPaths.spool,'service.json'),{schema:'daemon.after-turn-service.v1',status:'running',heartbeat_at:Date.now()/1000,worker_count:2,worker_errors:0,memory_root_ref:ref(f.row.daemon.runtime)});await f.write(path.join(f.row.healthPaths.spool,'failed/hermes/12345/one.json'),'private-dialogue');await f.write(path.join(f.row.daemon.runtime,'levels/registre_passes.jsonl'),JSON.stringify({faculte:'dreaming',user_id:'12345',status:'echec',ts:new Date().toISOString(),detail:'private-secret'})+'\n');const r=await collect(f.row);assert.equal(check(r,'worker').status,'ok');assert.equal(check(r,'model').status,'ok');assert.equal(check(r,'conversation').status,'unknown');assert.equal(check(r,'search').status,'unknown');assert.equal(check(r,'queue').status,'issue');assert.equal(check(r,'dreaming').status,'issue');assert.equal(check(r,'rem').status,'unknown');assert(!JSON.stringify(r).includes('private'));assert(!JSON.stringify(r).includes('12345'));assert(!JSON.stringify(r).includes(f.root));});
test('old success and stopped worker are not current verification',async t=>{const f=await fixture(t);await receipts(f,'completed',2*86400000);await f.write(path.join(f.row.healthPaths.spool,'service.json'),{status:'stopped',heartbeat_at:Date.now()/1000});const r=await collect(f.row);assert.equal(check(r,'model').status,'check');assert.equal(check(r,'worker').status,'check');});
test('wrong memory and account receipts never verify another installation',async t=>{const f=await fixture(t);await receipts(f,'completed',0,{memory_root_ref:'wrong'});assert.equal(check(await collect(f.row),'model').status,'unknown');await receipts(f,'completed',0,{user_ref:ref('other')});assert.equal(check(await collect(f.row),'model').status,'unknown');});
test('latest failure supersedes success and unreadable evidence stays unknown',async t=>{const f=await fixture(t);await receipts(f,'failed');assert.equal(check(await collect(f.row),'model').status,'issue');await fs.rm(f.row.healthPaths.receipts,{recursive:true});await fs.symlink(f.row.daemon.runtime,f.row.healthPaths.receipts,'dir');assert.equal(check(await collect(f.row),'model').status,'unknown');});
test('legacy host and new daemon do not falsely fail missing nightly history',async t=>{const f=await fixture(t);const r=await collect(f.row);assert.equal(check(r,'dreaming').status,'unknown');assert.equal(check(r,'rem').status,'unknown');assert.equal(r.status,'unknown');const bare=await collect({host:'openclaw',state:'unknown'});assert.equal(bare.status,'unknown');});


test('future heartbeat and nonnumeric worker counts cannot claim worker health',async t=>{
 const f=await fixture(t),file=path.join(f.row.healthPaths.spool,'service.json');
 const b={schema:'daemon.after-turn-service.v1',status:'running',heartbeat_at:Date.now()/1000+50,worker_count:2,worker_errors:0,memory_root_ref:ref(f.row.daemon.runtime)};
 await f.write(file,b);assert.equal(check(await collect(f.row),'worker').status,'check');
 await f.write(file,{...b,heartbeat_at:Date.now()/1000,worker_count:'2'});assert.equal(check(await collect(f.row),'worker').status,'check');
});

test('focused diagnostic links the failing stage, last success and scoped retry without leaking data',async t=>{
 const f=await fixture(t),memory=f.row.daemon.runtime,spool=f.row.healthPaths.spool,at=new Date(Date.now()-5000).toISOString(),before=new Date(Date.now()-10000).toISOString();
 const identity={user_ref:ref('12345'),platform:'telegram',memory_root_ref:ref(memory),source_root_ref:ref(path.dirname(f.row.daemon.source))};
 const events=[{stage:'cycle',status:'completed',at:before,turn_id:'old'},
 {stage:'memory',status:'failed',details:{error_type:'JSONDecodeError',secret:'never-export',message:'secret-token /private/path'},reason_code:'hook_failed'},
 {stage:'cycle',status:'failed'}, {stage:'outbox',status:'pending',reason_code:'retry_scheduled',details:{error_type:'JSONDecodeError'}}];
 await f.write(path.join(f.row.healthPaths.receipts,'one.jsonl'),events.map(e=>JSON.stringify({schema:'daemon.continuity-receipt.event.v1',turn_id:'test',at,identity,...e})).join('\n'));
 await f.write(path.join(spool,'runtime.json'),{schema:'daemon.after-turn-spool-runtime.v1',memory_root_ref:ref(memory)});
 await f.write(path.join(spool,'service.json'),{schema:'daemon.after-turn-service.v1',status:'running',heartbeat_at:Date.now()/1000,worker_count:2,worker_errors:0,memory_root_ref:ref(memory)});
 const file=path.join(spool,'pending/hermes/12345/test.json'),job={schema:'daemon.after-turn-job.v1',host:'hermes',user_id:'12345',turn_id:'test',attempts:1,available_at:Date.now()/1000+90,last_error:'JSONDecodeError',user_l0:{content:'never-export'}};
 await f.write(file,job);
 const r=await collect(f.row),c=check(r,'after-turn');assert.equal(c.status,'recovering');assert.equal(c.diagnostic.lastSuccessAt,before);assert.equal(c.diagnostic.events[0].error,'JSONDecodeError');assert.equal(c.diagnostic.recovery,'scheduled');
 const one=require('./health.cjs').focusedReport(r,'after-turn');assert.equal(one.checks.length,1);assert.equal(one.checks[0].key,'after-turn');assert.throws(()=>require('./health.cjs').focusedReport(r,'../../secret'));
 for(const forbidden of ['never-export','secret-token','12345',f.root,'turn_id','user_l0'])assert(!JSON.stringify(one).includes(forbidden));
 await f.write(path.join(spool,'runtime.json'),{schema:'daemon.after-turn-spool-runtime.v1',memory_root_ref:'other'});assert.equal(check(await collect(f.row),'after-turn').diagnostic.recovery,'unknown');
 await f.write(path.join(spool,'runtime.json'),{schema:'daemon.after-turn-spool-runtime.v1',memory_root_ref:ref(memory)});
 await f.write(path.join(spool,'failed/hermes/12345/failed.json'),{...job,last_error:'secret-token'});const failed=check(await collect(f.row),'queue');assert.equal(failed.status,'issue');assert.equal(failed.diagnostic.recovery,'unknown');assert(!JSON.stringify(failed).includes('secret-token'));
 await fs.rm(spool,{recursive:true});await receipts(f,'completed');assert.equal(check(await collect(f.row),'after-turn').status,'ok');
});
test('only an owned stopped Gateway offers a direct intervention',async t=>{
 const f=await fixture(t);f.row.state='stopped';f.row.managed=true;
 let c=check(await collect(f.row),'host');assert.equal(c.status,'action');assert.equal(c.diagnostic.guidance,'start-host');
 f.row.managed=false;c=check(await collect(f.row),'host');assert.notEqual(c.status,'action');assert.notEqual(c.diagnostic.guidance,'start-host');
});
test('invalid and symlinked job data never supplies a cause or recovery state',async t=>{
 const f=await fixture(t),spool=f.row.healthPaths.spool;await f.write(path.join(spool,'runtime.json'),{schema:'daemon.after-turn-spool-runtime.v1',memory_root_ref:ref(f.row.daemon.runtime)});
 await f.write(path.join(spool,'failed/hermes/12345/wrong.json'),{schema:'daemon.after-turn-job.v1',host:'openclaw',user_id:'12345',attempts:1,last_error:'PermissionError'});
 await f.write(path.join(f.root,'outside.json'),{last_error:'AuthenticationError'});await fs.symlink(path.join(f.root,'outside.json'),path.join(spool,'failed/hermes/12345/link.json'));
 assert.deepEqual(check(await collect(f.row),'queue').diagnostic.events,[]);
});

test('a skipped cycle never counts as a successful update',async t=>{const f=await fixture(t);await receipts(f,'skipped');assert.equal(check(await collect(f.row),'after-turn').status,'unknown');});

test('an invalid stopped Gateway never offers Start',async t=>{const f=await fixture(t);Object.assign(f.row,{managed:true,state:'stopped',problem:'unreadable'});const c=check(await collect(f.row),'host');assert.equal(c.status,'issue');assert.notEqual(c.diagnostic.guidance,'start-host');});

// Hermes Agent's own record of its Telegram connection (gateway_state.json).
async function hermesGateway(f,platforms,{age=0,startedAgo=600000,pid=4242,owner=4242}={}) {
  const profile=path.join(f.root,'profile');f.row.profile=profile;f.row.managed=true;
  await f.write(path.join(profile,'gateway.pid'),{pid:owner,kind:'hermes-gateway'});
  const started=new Date(Date.now()-startedAgo);await fs.utimes(path.join(profile,'gateway.pid'),started,started);
  await f.write(path.join(profile,'gateway_state.json'),{pid,kind:'hermes-gateway',gateway_state:'running',platforms,updated_at:new Date(Date.now()-age).toISOString()});
}
test('a running Hermes gateway without its Telegram platform asks for a restart',async t=>{
  const f=await fixture(t);await hermesGateway(f,{});
  const c=check(await collect(f.row),'conversation');
  assert.equal(c.detail,'channel-disconnected');assert.equal(c.status,'issue');assert.equal(c.diagnostic.guidance,'restart-host');
  f.row.managed=false;assert.equal(check(await collect(f.row),'conversation').diagnostic.guidance,'support');
});
test('a fresh Hermes gateway is still connecting, a connected one is ok',async t=>{
  const f=await fixture(t);await hermesGateway(f,{},{startedAgo:30000});
  assert.equal(check(await collect(f.row),'conversation').detail,'channel-connecting');
  await hermesGateway(f,{telegram:{state:'connected',updated_at:new Date().toISOString()}});
  const c=check(await collect(f.row),'conversation');assert.equal(c.status,'ok');assert.equal(c.detail,'channel-connected');
  await hermesGateway(f,{telegram:{state:'retrying',error_code:'telegram_network_error'}});
  assert.equal(check(await collect(f.row),'conversation').detail,'channel-connecting');
});
test('a rejected token and a second poller are named, not restarted',async t=>{
  const f=await fixture(t);
  await hermesGateway(f,{telegram:{state:'fatal',error_code:'telegram_auth_error',error_message:'secret-detail'}});
  let c=check(await collect(f.row),'conversation');assert.equal(c.detail,'channel-token-rejected');assert.equal(c.diagnostic.guidance,'bot-token');
  assert(!JSON.stringify(c).includes('secret-detail'));
  await hermesGateway(f,{telegram:{state:'fatal',error_code:'telegram_polling_conflict'}});
  c=check(await collect(f.row),'conversation');assert.equal(c.detail,'channel-conflict');assert.equal(c.diagnostic.guidance,'bot-conflict');
});
test('a stale, foreign or stopped Hermes record proves nothing about Telegram',async t=>{
  const f=await fixture(t);
  await hermesGateway(f,{},{age:20*60000});assert.equal(check(await collect(f.row),'conversation').detail,'channel-not-tested');
  await hermesGateway(f,{},{owner:999});assert.equal(check(await collect(f.row),'conversation').detail,'channel-not-tested');
  await hermesGateway(f,{});f.row.state='stopped';assert.equal(check(await collect(f.row),'conversation').detail,'channel-not-tested');
  f.row.state='running';f.row.binding={user_id:'12345',platform:'openclaw'};assert.equal(check(await collect(f.row),'conversation').detail,'channel-not-tested');
});
test('OpenClaw supplies its gateway answer; unknown states are ignored',async t=>{
  const f=await fixture(t);f.row.host='openclaw';f.row.managed=true;
  assert.equal(check(await collect(f.row,{channel:{state:'token-rejected',at:null}}),'conversation').detail,'channel-token-rejected');
  assert.equal(check(await collect(f.row,{channel:{state:'disconnected',at:null}}),'conversation').diagnostic.guidance,'restart-host');
  assert.equal(check(await collect(f.row,{channel:{state:'surprise'}}),'conversation').detail,'channel-not-tested');
});
