'use strict';
// Persist only enumerated technical events. Never record protocol answers,
// prompt text, URLs, native output, exception messages or personal paths.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const PHASES=['bootstrap','host','identity','model','telegram','installation','conversation'];
const OPERATIONS=['review','apply','upgrade-review','upgrade-apply','upgrade-recover','create-start','create-download','create-key','create-model','create-telegram','discard','api-key', 'embeddings', 'first-exchange', 'gateway', 'host-check', 'host-download', 'host-plan', 'model-check', 'model-test', 'recovery', 'runtime-install', 'sign-in', 'telegram-token','package','python','libraries','activity','runtime-layout','runtime-environment','identity','provider-state','hermes-profile','openclaw-profile','openclaw-api-service','openclaw-activation','background-services','hermes-activation','finalize-rollback','add-host','daemon-update','source-check','host-prepare','host-attach'];
const STATUSES=['started','completed','failed','cancelled','interrupted','waiting','installed','unfinished','error'];
const ERRORS=['attachment.unconfirmed','attachment.source-profile','attachment.source-recovery','attachment.source-runtime','attachment.source-environment','attachment.source-connection','attachment.destination-layout','attachment.credential-store','attachment.source-upgrade','attachment.creation','attachment.creation-key','attachment.creation-model','attachment.creation-telegram','attachment.rolled_back','attachment.rollback_failed','attachment.command_recovery','attachment.source','attachment.ownership','attachment.services','attachment.upgrade','attachment.runtime','attachment.activation','attachment.changed','discard.ownership','discard.completed','discard.shared','discard.services','discard.running','discard.changed','discard.memory','discard.unsafe','discard.cleanup','discard.platform','ApiConnectionError','TelegramSetupError','HTTPError','URLError','KeyError','TypeError','ValueError','RuntimeError','OSError','StopIteration','AttributeError','TimeoutError','ConnectionError','HostCommandError','HostCommandTimeout','TimeoutExpired','TransactionApplyError','TransactionError','FileNotFoundError','PermissionError','UnexpectedError','process-exit','invalid-frame','retry'];
// Exception class names come from code; any other unknown code stays UnexpectedError.
const ERROR_CLASS=/^(?:[A-Z][A-Za-z0-9]{0,47})?(?:Error|Exception|Refused|Timeout|Expired|Stopped|Failure|Unavailable|Iteration|Back|Quit)$/;
const validTime=x=>typeof x==='string'&&/^\d{4}-\d\d-\d\dT[0-9:.]+(?:Z|[+-]\d\d:\d\d)$/.test(x)&&Number.isFinite(Date.parse(x));
const AUTH_STAGES=['starting','waiting','exchanging','authenticated','catalog'];
const CHATGPT_MODELS=['gpt-6-astra','gpt-6.1-sol'];
function modelCatalog(value) {
  if(!value||value.status!=='ready'||value.raw_catalog_observed!==false||!Array.isArray(value.models)||value.models.length!==CHATGPT_MODELS.length)return null;
  if(!['native_count','excluded_count'].every(k=>Number.isInteger(value[k])&&value[k]>=0&&value[k]<=10000))return null;
  if(!value.models.every((m,i)=>m?.id===CHATGPT_MODELS[i]&&['offered','absent-native'].includes(m.status)))return null;
  if(value.native_count!==value.excluded_count+value.models.filter(m=>m.status==='offered').length)return null;
  return {status:'ready',raw_catalog_observed:false,native_count:value.native_count,excluded_count:value.excluded_count,models:value.models.map(m=>({id:m.id,status:m.status}))};
}
function event(value,phase,at=new Date().toISOString()) {
  const e={at:validTime(value.at)?value.at:at,phase:PHASES.includes(value.phase)?value.phase:phase,
    operation:OPERATIONS.includes(value.operation)?value.operation:'activity',status:STATUSES.includes(value.status)?value.status:'started'};
  if(['snapshot','apply','verify','rollback'].includes(value.transaction_phase))e.transaction_phase=value.transaction_phase;
  if(typeof value.error_code==='string')e.error_code=ERRORS.includes(value.error_code)||ERROR_CLASS.test(value.error_code)?value.error_code:'UnexpectedError';
  if(['source-profile','source-recovery','source-runtime','source-environment','source-connection','source-preflight','destination-layout','credential-store','creation','association','request','transport'].includes(value.attachment_stage))e.attachment_stage=value.attachment_stage;
  if(typeof value.exception_type==='string'&&(value.exception_type==='UnexpectedError'||ERROR_CLASS.test(value.exception_type)))e.exception_type=value.exception_type;
  if(typeof value.refusal==='string'&&/^[a-z][a-z0-9-]{0,40}$/.test(value.refusal))e.refusal=value.refusal;
  if(['environment-missing','environment-manifest-missing','environment-manifest-invalid','environment-manifest-mismatch','environment-content-changed','interpreter-missing','environment-link-invalid','path-not-direct','bridge-not-ready','host-services-changed','host-not-ready','maintenance-not-ready','host-not-stopped'].includes(value.failure_reason))e.failure_reason=value.failure_reason;
  if(['prepare','node-download','node-extract','node-check','dependencies','host-check'].includes(value.host_stage))e.host_stage=value.host_stage;
  for(const key of ['winerror','exit_code'])if(Number.isInteger(value[key])&&value[key]>=-2147483648&&value[key]<=2147483647)e[key]=value[key];
  if(Number.isInteger(value.errno)&&Math.abs(value.errno)<100000)e.errno=value.errno;
  if(Number.isInteger(value.timeout_seconds)&&value.timeout_seconds>0&&value.timeout_seconds<=86400)e.timeout_seconds=value.timeout_seconds;
  if(AUTH_STAGES.includes(value.auth_stage))e.auth_stage=value.auth_stage;
  const catalog=modelCatalog(value.model_catalog);if(catalog)e.model_catalog=catalog;
  return e;
}
function sanitize(data) {
  if(data?.schema!=='daemon.setup-diagnostic.v1'||!Array.isArray(data.events))throw new Error('No installation diagnostic is available yet.');
  const result={schema:data.schema,platform:['darwin','win32'].includes(data.platform)?data.platform:'unknown',
    revision:/^[a-f0-9]{7,64}$/.test(data.revision||'')?data.revision:'unknown',
    startedAt:validTime(data.startedAt)?data.startedAt:null,
    status:STATUSES.includes(data.status)?data.status:'interrupted',
    events:data.events.slice(-256).map(e=>event(e,'bootstrap'))};
  return result;
}
class Diagnostics {
  constructor(root,metadata={}) {this.root=root?path.resolve(root):null;this.metadata=metadata;this.data=null;this.failed=false;this.active=false;}
  begin() {
    this.data={schema:'daemon.setup-diagnostic.v1',platform:process.platform,revision:this.metadata.revision,
      startedAt:new Date().toISOString(),status:'started',events:[]};
    this.active=true;this.phase='bootstrap';this.failed=false;this.file=path.join(this.root||'.',Date.now()+'-'+crypto.randomUUID()+'.json');
    this.record({phase:'bootstrap',operation:'package',status:'started'});
  }
  record(value) {
    if(!this.active||!this.data)return;
    const e=event(value,this.phase);this.phase=e.phase;
    this.data.events.push(e);this.data.events=this.data.events.slice(-256);
    this.persist();
  }
  observe(frame) {
    if(!this.active)return;
    if(frame.type==='phase'&&PHASES.includes(frame.name))this.record({phase:frame.name,status:'started'});
    if(frame.type==='diagnostic')this.record(frame);
    if(frame.type==='prompt')this.record({status:'waiting',error_code:['API connection not ready','Telegram connection not ready'].includes(frame.title)?'retry':undefined});
    if(frame.type==='finished') {
      const status=STATUSES.includes(frame.result)?frame.result:'unfinished';
      this.record({status,error_code:frame.error_code});this.data.status=status;this.persist();this.active=false;
    }
  }
  persist() {
    let tmp;
    try {
      if(!this.root)throw new Error();
      let parent=this.root;while(!fs.existsSync(parent))parent=path.dirname(parent);
      if(fs.realpathSync(parent)!==parent)throw new Error();
      fs.mkdirSync(this.root,{recursive:true,mode:0o700});
      if(fs.realpathSync(this.root)!==this.root)throw new Error();
      tmp=this.file+'.tmp';fs.writeFileSync(tmp,JSON.stringify(sanitize(this.data),null,2)+'\n',{mode:0o600,flag:'wx'});
      fs.renameSync(tmp,this.file);this.failed=false;
      const files=fs.readdirSync(this.root).filter(n=>/^\d+-[a-f0-9-]+\.json$/.test(n)).sort();
      for(const name of files.slice(0,-20))fs.unlinkSync(path.join(this.root,name));
    } catch {this.failed=true;if(tmp)try{fs.unlinkSync(tmp);}catch{}}
  }
  read() {
    if(this.data)return {...sanitize(this.data),saved:!this.failed};
    try {
      if(fs.realpathSync(this.root)!==this.root)throw new Error();
      const files=fs.readdirSync(this.root).filter(n=>/^\d+-[a-f0-9-]+\.json$/.test(n)).sort();
      const file=path.join(this.root,files.at(-1)||'missing'),s=fs.lstatSync(file);
      if(!s.isFile()||s.isSymbolicLink()||s.size>131072)throw new Error();
      const d=sanitize(JSON.parse(fs.readFileSync(file,'utf8')));
      if(d.status==='started')d.status='interrupted';
      return {...d,saved:true};
    } catch {throw new Error('No installation diagnostic is available yet.');}
  }
}
module.exports={Diagnostics,sanitize,event,PHASES,OPERATIONS,AUTH_STAGES,modelCatalog};
