'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {Diagnostics,PHASES}=require('./diagnostics.cjs');
const {Backend}=require('./backend.cjs');
function fixture(t){const root=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'diagnostic-')));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));return root;}
for(const phase of PHASES)test('crash checkpoint survives reopening at '+phase,t=>{
 const root=fixture(t),d=new Diagnostics(root,{revision:'a'.repeat(40)});d.begin();d.observe({type:'phase',name:phase});
 d.observe({type:'prompt',kind:'secret',title:'private token',previous:'private-secret'});
 d.record({operation:'runtime-environment',transaction_phase:'apply',status:'failed',error_code:'FileNotFoundError',errno:2,message:'secret /Users/private',token:'do-not-export'});
 const reopened=new Diagnostics(root).read();assert.equal(reopened.status,'interrupted');assert.equal(reopened.events.at(-1).phase,phase);assert.equal(reopened.events.at(-1).errno,2);assert.equal(reopened.events.at(-1).transaction_phase,'apply');
 const bytes=JSON.stringify(reopened);for(const secret of ['private-secret','private token','/Users/private','do-not-export'])assert(!bytes.includes(secret));
});
test('cancellation, success, retention and malicious saved keys are bounded',t=>{
 const root=fixture(t),d=new Diagnostics(root);for(let i=0;i<25;i++){d.begin();d.observe({type:'finished',result:i===24?'installed':'cancelled'});}
 assert.equal(fs.readdirSync(root).length,20);assert.equal(d.read().status,'installed');
 const latest=d.file,data=JSON.parse(fs.readFileSync(latest));data.secret='private-secret';data.events.push({at:'private-secret',phase:'private-secret',error_code:'private-secret',status:'failed'});fs.writeFileSync(latest,JSON.stringify(data));
 assert(!JSON.stringify(new Diagnostics(root).read()).includes('private-secret'));
});
test('disk failure retains a report in memory and redirected directories are refused',t=>{
 const root=fixture(t),target=path.join(root,'outside');fs.mkdirSync(target);const link=path.join(root,'link');fs.symlinkSync(target,link,'dir');
 const d=new Diagnostics(link);d.begin();assert.equal(d.read().saved,false);assert.equal(fs.readdirSync(target).length,0);
});
test('real child exit and bootstrap failure retain stage without stdout or stderr',async t=>{
 const root=fixture(t),events=[],b=new Backend({bootstrapHome:root,installHome:root,emit:e=>events.push(e)});
 b.prepare=async()=>({python:process.execPath,args:['-e',"process.stdout.write(JSON.stringify({schema:'daemon.desktop.v1',type:'phase',name:'telegram'})+'\\n');process.stderr.write('secret-native-output');process.exit(2)"],env:{}});
 await b.start();await b.stopped;assert.equal(b.diagnostics.read().events.at(-1).phase,'telegram');assert.equal(b.diagnostics.read().events.at(-1).error_code,'process-exit');
 b.prepare=async()=>{throw new Error('private-bootstrap-error');};await b.start();await b.stopped;const report=b.diagnostics.read();assert.equal(report.status,'error');assert.equal(report.events.at(-1).phase,'bootstrap');assert(!JSON.stringify(report).includes('private-'));assert(!JSON.stringify(report).includes('secret-'));
});


test('a symlinked parent cannot create report directories outside the app root',t=>{
 const root=fixture(t),target=path.join(root,'outside');fs.mkdirSync(target);fs.symlinkSync(target,path.join(root,'redirect'),'dir');
 const d=new Diagnostics(path.join(root,'redirect','new'));d.begin();assert.equal(d.read().saved,false);assert.deepEqual(fs.readdirSync(target),[]);
});

test('host diagnostics retain operation and bounded error, never request secrets',()=>{
 const {sanitize}=require('./diagnostics.cjs');
 const report=sanitize({schema:'daemon.setup-diagnostic.v1',events:[{phase:'installation',operation:'create-model',status:'failed',error_code:'attachment.creation-model',key:'synthetic-secret',source_profile:'/private/source'},{operation:'upgrade-apply',status:'failed',error_code:'attachment.rolled_back'},{operation:'create-key',status:'failed',error_code:'secret exception synthetic-secret'}]});
 assert.equal(report.events[0].operation,'create-model');
 assert.equal(report.events[0].error_code,'attachment.creation-model');
 assert.equal(report.events[1].error_code,'attachment.rolled_back');
 assert.equal(report.events[2].error_code,'UnexpectedError');
 const classes=sanitize({schema:'daemon.setup-diagnostic.v1',events:[{operation:'source-check',status:'failed',error_code:'UnicodeDecodeError',exception_type:'Refused',refusal:'upgrade'},{operation:'host-prepare',status:'failed',error_code:'Sk1234secret',exception_type:'private secret',refusal:'Private Secret'}]});
 assert.deepEqual([classes.events[0].operation,classes.events[0].error_code,classes.events[0].exception_type,classes.events[0].refusal],['source-check','UnicodeDecodeError','Refused','upgrade']);
 assert.equal(classes.events[1].error_code,'UnexpectedError');assert.ok(!Object.hasOwn(classes.events[1],'exception_type')&&!Object.hasOwn(classes.events[1],'refusal'));
 assert.ok(!JSON.stringify(report).includes('synthetic-secret'));
 assert.ok(!JSON.stringify(report).includes('/private/source'));
});


test('timeouts retain an explicit type and limit without arbitrary values',()=>{
 const {sanitize}=require('./diagnostics.cjs');
 const r=sanitize({schema:'daemon.setup-diagnostic.v1',events:[{phase:'host',status:'failed',error_code:'HostCommandTimeout',timeout_seconds:1800,output:'private output'},{timeout_seconds:'secret'}]});
 assert.equal(r.events[0].error_code,'HostCommandTimeout');assert.equal(r.events[0].timeout_seconds,1800);
 assert(!JSON.stringify(r).includes('private output'));assert(!Object.hasOwn(r.events[1],'timeout_seconds'));
});

test('submitting a displayed question resumes the diagnostic without storing its answer',t=>{
 const root=fixture(t),b=new Backend({bootstrapHome:root,installHome:root,emit:()=>{}});
 b.diagnostics.begin();b.active=true;b.child={stdin:{write:()=>{}}};
 b.event({type:'prompt',id:1,kind:'secret',maximum:100,title:'Synthetic key'});
 assert.equal(b.diagnostics.read().events.at(-1).status,'waiting');
 b.answer({id:1,action:'submit',value:'private-answer'});
 assert.equal(b.diagnostics.read().events.at(-1).status,'started');
 assert(!JSON.stringify(b.diagnostics.read()).includes('private-answer'));
});

test('host stage and numeric Windows details survive export without private text',()=>{
 const {sanitize}=require('./diagnostics.cjs');
 const r=sanitize({schema:'daemon.setup-diagnostic.v1',events:[{phase:'host',status:'failed',host_stage:'node-extract',winerror:206,exit_code:1,message:'secret path'},{host_stage:'secret path',winerror:'private',exit_code:NaN}]});
 assert.equal(r.events[0].host_stage,'node-extract');assert.equal(r.events[0].winerror,206);assert.equal(r.events[0].exit_code,1);
 assert(!JSON.stringify(r).includes('secret'));assert(!Object.hasOwn(r.events[1],'winerror'));
});

test('attachment preflight details survive export, arbitrary text does not',()=>{
 const {sanitize}=require('./diagnostics.cjs');
 const r=sanitize({schema:'daemon.setup-diagnostic.v1',events:[{phase:'installation',operation:'create-start',status:'failed',error_code:'attachment.source-environment',attachment_stage:'source-environment',exception_type:'RuntimeError',failure_reason:'environment-content-changed',winerror:206,message:'private-secret'},{attachment_stage:'private-secret',exception_type:'private-secret',failure_reason:'private-secret'}]});
 assert.equal(r.events[0].attachment_stage,'source-environment');assert.equal(r.events[0].failure_reason,'environment-content-changed');assert.equal(r.events[0].winerror,206);
 assert(!JSON.stringify(r).includes('private-secret'));assert(!Object.hasOwn(r.events[1],'exception_type'));
});


test('model evidence survives export without provider metadata or codes',t=>{
 const root=fixture(t),d=new Diagnostics(root);d.begin();
 const model_catalog={status:'ready',raw_catalog_observed:false,native_count:2,excluded_count:1,token:'never-save',models:['gpt-6-astra','gpt-6.1-sol'].map((id,i)=>({id,status:i===1?'offered':'absent-native',private:'never-save'}))};
 d.observe({type:'diagnostic',phase:'model',operation:'model-check',status:'completed',model_catalog,auth_stage:'catalog',code:'TEST-0000'});
 const exported=d.read();assert.equal(exported.events.at(-1).model_catalog.models[0].status,'absent-native');
 const raw=JSON.stringify(exported);assert(!raw.includes('never-save'));assert(!raw.includes('TEST-0000'));
 const restored=new Diagnostics(root).read();assert.deepEqual(restored.events,exported.events);
 d.observe({type:'diagnostic',model_catalog:{...model_catalog,native_count:123456},auth_stage:'secret'});
 assert(!d.read().events.at(-1).model_catalog);assert(!d.read().events.at(-1).auth_stage);
});
