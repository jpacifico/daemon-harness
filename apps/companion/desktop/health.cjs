'use strict';
// Read-only evidence collector. No provider calls, host execution, note bodies,
// credentials, repair or scheduling. Only allowlisted metadata leaves this process.
const fs=require('node:fs/promises'),path=require('node:path'),crypto=require('node:crypto');
const ref=value=>crypto.createHash('sha256').update(value).digest('hex').slice(0,16);
const time=v=>{const n=typeof v==='number'?v*1000:Date.parse(v);return Number.isFinite(n)&&n>0&&n<=Date.now()+60000?new Date(n).toISOString():null;};
async function direct(file) {return path.resolve(await fs.realpath(file))===path.resolve(file);}
async function read(file,limit=262144,tail=false) {
  if(!file||!await direct(file))throw new Error('unavailable');
  const h=await fs.open(file,'r');
  try {const s=await h.stat();if(!s.isFile()||(!tail&&s.size>limit))throw new Error('unavailable');
    const start=Math.max(0,s.size-limit),b=Buffer.alloc(Math.min(s.size,limit));await h.read(b,0,b.length,start);
    const raw=b.toString('utf8');return start?raw.slice(raw.indexOf('\n')+1):raw;
  } finally {await h.close();}
}
async function json(file){try{return JSON.parse(await read(file));}catch{return null;}}
async function lines(file){try{return (await read(file,262144,true)).split('\n').flatMap(line=>{try{const d=JSON.parse(line);return d&&typeof d==='object'?[d]:[];}catch{return [];}});}catch{return [];}}
const errors=new Set(['Error','RuntimeError','ValueError','TypeError','KeyError','OSError','FileNotFoundError','PermissionError','TimeoutError','TimeoutExpired','ConnectionError','ConnectionRefusedError','JSONDecodeError','ValidationError','HTTPError','HTTPStatusError','APIError','APIConnectionError','APITimeoutError','AuthenticationError','PermissionDeniedError','RateLimitError','BadRequestError','NotFoundError','InternalServerError','ProviderError','HostCommandError','IncompleteRead','CancelledError']);
const reasons=new Set(['hook_failed','writer_failed','thread_start_failed','after_turn_failed','after_turn_incomplete','after_turn_completed','retry_scheduled','attempts_exhausted','worker_claimed','terminal_retry_requeued','projection_retry_scheduled','projection_attempts_exhausted','projection_claimed','projection_split_recovery_scheduled','l0_reference_unrecoverable','retries_exhausted','memory_dependency_failed','l0_dependency_failed','durable_projection_deferred','stale_lease_requeued','receipt_already_completed']);
const stages=new Set(['response','cycle','l0','l0_user','l0_assistant','memory','scene_schedule','scene','outbox','compose']);
function evidence(e) {
  const at=time(e.at);if(!at||!stages.has(e.stage)||!['completed','failed','skipped','pending','started','recovered'].includes(e.status))return null;
  return {stage:e.stage,status:e.status,at,
    ...(reasons.has(e.reason_code)?{reason:e.reason_code}:{}),
    ...(errors.has(e.details?.error_type)?{error:e.details.error_type}:{}),
    degraded:e.details?.degraded===true};
}
async function receiptEvidence(root,memory,source,binding) {
  if(!root||!binding)return {};
  try {
    if(!await direct(root))return {};
    const candidates=[];const directory=await fs.opendir(root);let visited=0;
    for await(const entry of directory) {if(++visited>2000)break;if(!entry.isFile()||!entry.name.endsWith('.jsonl'))continue;const s=await fs.lstat(path.join(root,entry.name));candidates.push({name:entry.name,at:s.mtimeMs});}
    const names=candidates.sort((a,b)=>b.at-a.at).slice(0,32).map(x=>x.name);
    const result={lastSuccess:{}};
    for(const name of names) {
      const turns=new Map();
      for(const e of await lines(path.join(root,name))) {
        if(e.schema!=='daemon.continuity-receipt.event.v1'||typeof e.turn_id!=='string')continue;
        const t=turns.get(e.turn_id)||{identity:{},events:[]};
        if(e.identity&&typeof e.identity==='object')Object.assign(t.identity,e.identity);
        t.events.push(e);turns.set(e.turn_id,t);
      }
      for(const t of turns.values()) {
        if(t.identity.user_ref!==ref(binding.user_id)||t.identity.platform!==binding.platform||t.identity.memory_root_ref!==ref(memory||'')||t.identity.source_root_ref!==ref(source?path.dirname(source):''))continue;
        const latest={};
        for(const raw of t.events) {const e=evidence(raw);if(e&&(!latest[e.stage]||e.at>=latest[e.stage].at))latest[e.stage]=e;}
        for(const e of t.events.map(evidence).filter(Boolean)) {
          if(e.status==='completed'&&!e.degraded&&(!result.lastSuccess[e.stage]||e.at>result.lastSuccess[e.stage]))result.lastSuccess[e.stage]=e.at;
        }
        for(const e of Object.values(latest)) {
          if(!['completed','failed','skipped'].includes(e.status))continue;
          if(result[e.stage]?.at>e.at)continue;
          result[e.stage]={...e,turnId:t.events[0].turn_id,
            events:Object.values(latest).filter(x=>x.status==='failed'||x.degraded).sort((a,b)=>a.at.localeCompare(b.at)).slice(-8)};
        }
      }
    }
    return result;
  }catch{return {};}
}
// Hermes Agent writes each messaging platform's state to its gateway_state.json
// and refreshes the file about every minute. A Telegram platform missing there
// never started: after a slow boot its plugin can overrun Hermes's load deadline,
// leaving a running gateway that no longer listens to Telegram.
const CHANNEL_GRACE_MS=120000,STATE_FRESH_MS=600000;
async function hermesChannel(row,now=Date.now()) {
  if(row.host!=='hermes'||row.binding?.platform!=='telegram'||!row.profile||row.state!=='running')return null;
  const state=await json(path.join(row.profile,'gateway_state.json')),owner=await json(path.join(row.profile,'gateway.pid'));
  if(state?.kind!=='hermes-gateway'||state.gateway_state!=='running'||!Number.isInteger(state.pid)||owner?.pid!==state.pid)return null;
  const updated=Date.parse(state.updated_at);
  if(!Number.isFinite(updated)||now-updated>STATE_FRESH_MS)return null;
  let started=null;try{started=(await fs.stat(path.join(row.profile,'gateway.pid'))).mtimeMs;}catch{return null;}
  // Hermes keeps platform entries across restarts; only one stamped by the
  // running process describes its connection, as in Hermes's own status page.
  const entry=state.platforms&&typeof state.platforms==='object'?state.platforms.telegram:null;
  const t=entry&&typeof entry==='object'&&Number.isInteger(state.start_time)
    &&entry.writer_pid===state.pid&&entry.writer_start_time===state.start_time?entry:null;
  if(t){
    const at=time(t.updated_at);
    if(t.state==='connected')return {state:'connected',at};
    if(t.error_code==='telegram_auth_error')return {state:'token-rejected',at};
    if(t.error_code==='telegram_polling_conflict')return {state:'conflict',at};
    // Hermes retries network failures itself.
    if(['connecting','retrying'].includes(t.state))return {state:'connecting',at};
    return {state:'disconnected',at};
  }
  return now-started<CHANNEL_GRACE_MS?{state:'connecting',at:null}:{state:'disconnected',at:new Date(started).toISOString()};
}
const CHANNEL_CHECKS={connected:['ok','channel-connected'],connecting:['recovering','channel-connecting'],
  disconnected:['action','channel-disconnected'],'token-rejected':['action','channel-token-rejected'],conflict:['action','channel-conflict']};
async function collect(row,{channel=null}={}) {
  const now=new Date().toISOString(),checks=[];
  const add=(key,status,detail,at=null,extra={})=>checks.push({key,status,detail,at,...extra});
  add('host',row.problem?'action':row.state==='running'?'ok':row.state==='stopped'?'check':'unknown',
    row.problem?'configuration-unreadable':row.state==='running'?'process-running':row.state==='stopped'?'process-stopped':'service-unavailable',now);
  const memory=row.daemon?.runtime,locations=row.healthPaths||{};
  let accessible=false;
  if(memory)try{accessible=await direct(memory)&&(await fs.stat(memory)).isDirectory();await fs.access(memory,fs.constants.R_OK|fs.constants.X_OK);}catch{accessible=false;}
  add('memory',memory?(accessible?'ok':'action'):'unknown',accessible?'folder-readable':'folder-unavailable',now);
  const receipt=await receiptEvidence(locations.receipts,memory,row.daemon?.source,row.binding);
  add('plugin',receipt.response?'ok':'unknown',receipt.response?'receipt-observed':row.daemon?'plugin-configured':'plugin-unverified',receipt.response?.at||null);
  for(const [key,stage] of [['model','response'],['after-turn','cycle']]) {
    const e=receipt[stage];
    add(key,e?(e.status==='skipped'?'unknown':e.status==='failed'||e.degraded?'action':Date.now()-Date.parse(e.at)>86400000?'check':'ok'):'unknown',
      e?(e.status==='skipped'?'operation-incomplete':e.status==='failed'||e.degraded?'latest-operation-failed':'last-success'):'no-receipt',e?.at||null);
  }
  // The host's own report of its Telegram connection; OpenClaw's arrives from its gateway query.
  const telegram=channel&&CHANNEL_CHECKS[channel.state]?channel:await hermesChannel(row);
  if(telegram&&CHANNEL_CHECKS[telegram.state])add('conversation',...CHANNEL_CHECKS[telegram.state],telegram.at||now);
  else add('conversation','unknown',row.binding?'channel-not-tested':'channel-unidentified');
  const heartbeat=locations.spool?await json(path.join(locations.spool,'service.json')):null;
  const beat=time(heartbeat?.heartbeat_at);
  const good=heartbeat?.schema==='daemon.after-turn-service.v1'&&heartbeat.memory_root_ref===ref(memory||'')&&heartbeat.status==='running'&&Number.isInteger(heartbeat.worker_count)&&heartbeat.worker_count>=2&&heartbeat.worker_errors===0&&beat&&Math.abs(Date.now()-Date.parse(beat))<15000;
  add('worker',good?'ok':heartbeat?'check':'unknown',good?'heartbeat-current':heartbeat?'heartbeat-stale':'no-heartbeat',beat);
  // Count names; inspect at most 32 job records for allowlisted structural metadata.
  let count=0,failed=0,oldest=null,limited=false,readable=false;const jobs=[];let metadataReads=0;const counts={pending:0,running:0,failed:0};
  if(locations.spool&&row.binding&&/^[A-Za-z0-9_-]{1,80}$/.test(row.binding.user_id)) {
    try {if(!await direct(locations.spool))throw new Error();readable=true;}catch{}
    if(readable)for(const state of ['pending','running','failed','scene_pending','scene_running','scene_failed']) {
      const folder=path.join(locations.spool,state,row.host,row.binding.user_id);
      try {if(!await direct(folder)) {limited=true;continue;}
        const names=(await fs.readdir(folder)).filter(n=>n.endsWith('.json'));if(names.length>1000)limited=true;
        for(const name of names.slice(0,1000)) {const s=await fs.lstat(path.join(folder,name));if(!s.isFile()||s.isSymbolicLink())continue;count++;if(state.endsWith('failed'))failed++;oldest=oldest===null?s.mtimeMs:Math.min(oldest,s.mtimeMs);
          const phase=state.replace('scene_','');counts[phase]++;
          if(metadataReads++<32){const j=await json(path.join(folder,name));
            if(j&&['daemon.after-turn-job.v1','daemon.scene-projection-job.v1','daemon.scene-projection-job.v2'].includes(j.schema)&&j.host===row.host&&j.user_id===row.binding.user_id&&Number.isInteger(j.attempts)&&j.attempts>=0&&j.attempts<100000){
              jobs.push({state:phase,projection:state.startsWith('scene_'),turnId:j.turn_id,attempts:j.attempts,
                availableAt:typeof j.available_at==='number'&&Number.isFinite(j.available_at)&&j.available_at>0&&j.available_at<4102444800?new Date(j.available_at*1000).toISOString():null,
                at:new Date(s.mtimeMs).toISOString(),...(errors.has(j.last_error)?{error:j.last_error}:{})});
            }
          }}
      }catch(e){if(e.code!=='ENOENT')limited=true;}
    }
  }
  add('queue',!readable||limited?'unknown':failed?'action':oldest&&Date.now()-oldest>300000?'check':'ok',!readable?'queue-unavailable':limited?'bounded-scan':failed?'queue-failed':count?'queue-pending':'queue-empty',now,{count,failed,counts});
  const passes=memory&&row.binding?await lines(path.join(memory,'levels/registre_passes.jsonl')):[];
  const maintenance={};
  for(const key of ['dreaming','rem']) {
    const p=passes.filter(p=>p.faculte===key&&p.user_id===row.binding?.user_id&&['ok','echec','skip'].includes(p.status)&&time(p.ts)).sort((a,b)=>Date.parse(b.ts)-Date.parse(a.ts))[0];
    maintenance[key]={lastSuccessAt:passes.filter(x=>x.faculte===key&&x.user_id===row.binding?.user_id&&x.status==='ok'&&time(x.ts)).map(x=>time(x.ts)).sort().at(-1)||null};
    add(key,!p?'unknown':p.status==='echec'?'action':p.status==='skip'?'unknown':Date.now()-Date.parse(p.ts)>48*3600000?'check':'ok',
      !p?'no-maintenance-receipt':p.status==='echec'?'maintenance-failed':p.status==='skip'?'maintenance-skipped':'last-maintenance-success',p?time(p.ts):null);
  }
  // No synthetic search is injected into personal memory just to get a green badge.
  add('search','unknown','search-not-tested');
  // The namespace prevents an unrelated spool from explaining this memory's incident.
  const namespace=locations.spool?await json(path.join(locations.spool,'runtime.json')):null;
  const bound=namespace?.schema==='daemon.after-turn-spool-runtime.v1'&&namespace.memory_root_ref===ref(memory||'');
  if(namespace&&!bound){const q=checks.find(c=>c.key==='queue');q.status='unknown';q.detail='queue-unavailable';delete q.count;delete q.failed;delete q.counts;}
  const safeJob=j=>({stage:j.projection?'scene':'outbox',status:j.state,at:j.at,attempts:j.attempts,availableAt:j.availableAt,...(j.error?{error:j.error}:{})});
  for(const c of checks) {
    const e=receipt[c.key==='model'?'response':c.key==='after-turn'?'cycle':''];
    const related=bound?jobs.filter(j=>c.key==='queue'||(e&&j.turnId===e.turnId)):[];
    const next=related.filter(j=>j.state==='pending'&&j.attempts>0&&j.availableAt&&Date.parse(j.availableAt)>Date.now()).sort((a,b)=>a.availableAt.localeCompare(b.availableAt))[0];
    const unresolvedFailure=related.some(j=>j.state==='failed')||(c.key==='queue'&&failed>0);
    const recovery=good&&!limited&&metadataReads<=32&&!unresolvedFailure&&next?'scheduled':good&&!limited&&metadataReads<=32&&!unresolvedFailure&&related.some(j=>j.state==='running')?'running':'unknown';
    c.diagnostic={impact:c.key,recovery,nextAttemptAt:recovery==='scheduled'?next.availableAt:null,
      lastSuccessAt:maintenance[c.key]?.lastSuccessAt||receipt.lastSuccess?.[c.key==='model'?'response':c.key==='after-turn'?'cycle':'']||null,
      events:c.key==='queue'?related.slice(0,8).map(safeJob):e?c.key==='model'?[{stage:e.stage,status:e.status,at:e.at,...(e.error?{error:e.error}:{}),...(e.reason?{reason:e.reason}:{})}]:e.events:[],
      limited:(limited||metadataReads>32)&&['queue','after-turn'].includes(c.key),
      guidance:c.key==='host'&&row.managed&&!row.problem&&row.state==='stopped'?'start-host'
        :c.key==='conversation'&&c.detail==='channel-disconnected'?(row.managed?'restart-host':'support')
        :c.key==='conversation'&&c.detail==='channel-token-rejected'?'bot-token'
        :c.key==='conversation'&&c.detail==='channel-conflict'?'bot-conflict'
        :c.key==='conversation'?'conversation':recovery==='scheduled'||recovery==='running'?'wait':c.status==='action'?'support':'inspect'};
    if(c.status==='action')c.status='issue';
    if(['after-turn','queue'].includes(c.key)&&['scheduled','running'].includes(recovery)&&c.status!=='ok')c.status='recovering';
    if(c.diagnostic.guidance==='start-host')c.status='action';
  }
  const status=checks.some(c=>c.status==='action')?'action':checks.some(c=>c.status==='issue')?'issue':checks.some(c=>c.status==='recovering')?'recovering':checks.some(c=>c.status==='check')?'check':checks.some(c=>c.status==='unknown')?'unknown':'ok';
  return {schema:'daemon.health-report.v2',host:row.host,checkedAt:now,status,checks};
}
function focusedReport(report,key) {
  const check=report?.checks.find(c=>c.key===key);
  if(!check)throw new Error('Unknown health check.');
  return {schema:'daemon.health-diagnostic.v1',host:report.host,checkedAt:report.checkedAt,status:check.status,checks:[check]};
}
module.exports={collect,receiptEvidence,focusedReport,hermesChannel};
