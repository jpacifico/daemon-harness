'use strict';
// Opt-in packaged qualification. Only the native confirmation choice is automated;
// inventory, renderer IPC, Python transaction and native services remain product code.
const fs=require('node:fs/promises'), path=require('node:path');
const {spawn}=require('node:child_process');
const {createInterface}=require('node:readline');
const {app,dialog}=require('electron');
const assert=require('node:assert/strict');
async function run(window,backend,instances){
  assert(app.isPackaged,'Run this qualification from the packaged app');
  const root=app.getPath('userData'),started=performance.now(),report={revision:backend.metadata.revision,platform:process.platform,packaged:true};
  let child,closed,originalDialog=dialog.showMessageBox;
  const evaluate=code=>window.webContents.executeJavaScript(code);
  const wait=async(expression,timeout=120000)=>{
    const deadline=Date.now()+timeout;
    while(!await evaluate(expression)){
      const error=await evaluate('typeof state!=="undefined" && state.error');
      if(error)throw new Error('UI error while waiting for '+expression+': '+error);
      if(Date.now()>deadline)throw new Error('UI timeout: '+expression+'; '+await evaluate('document.querySelector("[role=alert]")?.textContent'));
      await new Promise(resolve=>setTimeout(resolve,100));
    }
  };
  const capture=async(name)=>{
    await new Promise(resolve=>setTimeout(resolve,250));
    assert(await evaluate('document.documentElement.scrollWidth<=innerWidth'));
    assert(await evaluate('Array.from(document.querySelectorAll(".memory-path")).every(p=>p.scrollWidth<=p.clientWidth+1)'), 'The complete memory path must fit');
    await fs.writeFile(path.join(root,name+'.png'),(await window.webContents.capturePage()).toPNG());
  };
  const click=selector=>evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
  const phase=name=>console.log(JSON.stringify({phase:name,root}));
  try{
    instances.environment={};
    // Native inventory is real, but test discovery must not traverse profiles of
    // personal services found in the Windows per-user scheduler.
    const nativeList=instances.services.list.bind(instances.services);
    instances.services.list=async()=>{const snapshot=await nativeList();return {...snapshot,items:snapshot.items.filter(s=>s.cwd===instances.base||s.cwd?.startsWith(instances.base+path.sep))};};
    await fs.mkdir(instances.home,{recursive:true});
    backend.environment=Object.fromEntries(Object.entries(process.env).filter(([key])=>['PATH','SYSTEMROOT','WINDIR','COMSPEC','PATHEXT','TEMP','TMP','TMPDIR'].includes(key.toUpperCase())));
    Object.assign(backend.environment,{HOME:instances.home,USERPROFILE:instances.home,APPDATA:path.join(instances.home,'AppData/Roaming'),LOCALAPPDATA:path.join(instances.home,'AppData/Local'),LANG:'en_US.UTF-8'});
    phase('private-python');
    backend.settingsRuntime=await backend.prepare(new AbortController().signal,{settingsOnly:true});
    const runtime=backend.settingsRuntime,bundle=path.join(backend.packageRoot,'payload');
    const script="import runpy,sys;sys.path[:0]=[sys.argv.pop(1),sys.argv.pop(1),sys.argv.pop(1)];runpy.run_module('scripts.companion_attachment_ui_fixture',run_name='__main__')";
    child=spawn(runtime.python,['-I','-B','-u','-c',script,bundle,path.join(bundle,'src'),runtime.libraries,'--base',instances.base,'--bundle',bundle,'--uv',path.join(backend.packageRoot,'bin',process.platform==='win32'?'uv.exe':'uv')],{env:runtime.env,stdio:['pipe','pipe','pipe']});
    closed=new Promise(resolve=>child.once('close',resolve));
    let stderr='';child.stderr.on('data',b=>{stderr+=b;});child.stdin.on('error',()=>{});
    const queue=[],readers=[];
    createInterface({input:child.stdout}).on('line',line=>{
      let value;try{value=JSON.parse(line);}catch{return;}
      if(readers.length)readers.shift()(value);else queue.push(value);
    });
    const next=async(timeout=600000)=>{
      let timer;
      try{return await Promise.race([queue.length?Promise.resolve(queue.shift()):new Promise(resolve=>readers.push(resolve)),closed.then(code=>{throw new Error('Fixture exited '+code+': '+stderr.slice(-2000));}),new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('Fixture timed out')),timeout);})]);}
      finally{clearTimeout(timer);}
    };
    const command=async(name)=>{child.stdin.write(JSON.stringify({command:name})+'\n');const result=await next();assert.notEqual(result.phase,'failed',JSON.stringify(result));return result;};
    phase('native-fixture');
    const ready=await next();assert.equal(ready.phase,'ready',JSON.stringify(ready));report.fixture=ready;
    let answer=0;const dialogs=[];
    dialog.showMessageBox=async(_window,options)=>{assert.equal(options.type,'question');assert.equal(options.defaultId,0);dialogs.push(options.message);return {response:answer};};
    assert(await backend.setupHomes.installed(backend.installHome),'The synthetic source receipt must represent a completed installation');
    phase('renderer-review');
    await evaluate('refreshInstances()');
    await wait('!!document.querySelector("[data-host-add]")');
    await click('[data-host-add]');
    await wait('!hostAdditionState.busy && !!document.querySelector("[data-host-target]")');
    await click('[data-host-target]');
    await wait('!hostAdditionState.busy && !!hostAdditionState.review');
    await capture('01-review-en');
    await click('[data-action=host-add-activate]');
    await wait('!hostAdditionState.busy');
    assert(!await evaluate('hostAdditionState.done'));
    report.cancel=await command('cancelled');
    await evaluate("document.querySelector('[data-language]').value='fr';document.querySelector('[data-language]').dispatchEvent(new Event('change'))");
    await wait("document.documentElement.lang==='fr'");
    await capture('02-review-fr');
    answer=1;phase('native-activation');const activation=performance.now();
    await click('[data-action=host-add-activate]');
    await wait('hostAdditionState.busy');
    await capture('03-activation');
    await wait('!hostAdditionState.busy',180000);
    assert(await evaluate('hostAdditionState.done'),await evaluate('state.error'));
    report.activation_seconds=(performance.now()-activation)/1000;
    const exported=await evaluate("window.companion.exportDiagnostic('host',hostAdditionState.id)");
    assert.equal(exported.report.status,'completed');
    assert(exported.report.events.some(e=>e.operation==='apply'&&e.status==='completed'));
    assert.equal(exported.report.revision,backend.metadata.revision);
    report.host_diagnostic_export=true;
    await capture('04-active');
    await click('[data-action=host-add-back]');
    await wait('!state.instanceBusy && state.inventory?.instances.filter(r=>r.daemon).length===2');
    assert.equal(await evaluate('document.querySelectorAll("[data-daemon-card]").length'),1);
    assert.equal(await evaluate('new Set(state.inventory.instances.filter(r=>r.daemon).map(r=>r.daemon.memory)).size'),1);
    await capture('05-one-daemon-two-hosts');
    await window.webContents.reload();
    await wait('!state.instanceBusy && state.inventory?.instances.filter(r=>r.daemon).length===2');
    assert.equal(await evaluate('document.querySelectorAll("[data-daemon-card]").length'),1);
    report.rediscovery=true;
    phase('gateway-control');
    const target=(await instances.refresh()).instances.find(r=>r.host==='openclaw');
    assert.equal(target.state,'running');
    for(const action of ['connect','restart']){
      const selector=`[data-instance="${target.id}"][data-instance-action="${action}"]`;
      await evaluate(`document.querySelector(${JSON.stringify(selector)}).closest('details').open=true`);
      await click(selector);
      await wait('!state.instanceBusy');
      assert.equal(await evaluate('state.error'),'');
    }
    phase('native-memory-read');report.native=await command('verify');
    assert.equal(report.native.phase,'verified');
    report.dialogs=dialogs;report.result='passed';
    return report;
  }catch(error){report.result='failed';report.error=error.stack;await capture('failure').catch(()=>{});throw error;}
  finally{
    dialog.showMessageBox=originalDialog;
    if(child && child.exitCode===null){child.stdin.end(JSON.stringify({command:'stop'})+'\n');await closed;}
    report.seconds=(performance.now()-started)/1000;
    report.cleanup=JSON.parse(await fs.readFile(path.join(root,'fixture-result.json'),'utf8').catch(()=>'{}'));
    if(!report.cleanup.ports_closed || !report.cleanup.worker_stopped || !report.cleanup.target_jobs_removed || (report.result==='passed' && !report.cleanup.rollback_verified))report.result='failed';
    await fs.writeFile(path.join(root,'packaged-result.json'),JSON.stringify(report,null,2)+'\n');
    console.log(JSON.stringify({phase:'finished',root,result:report.result,cleanup:report.cleanup}));
    if(child)assert(report.result==='passed', 'Native activation or cleanup failed; see packaged-result.json');
    if(child)assert(report.cleanup.ports_closed && report.cleanup.worker_stopped && report.cleanup.target_jobs_removed,'Fixture cleanup incomplete');
  }
}
module.exports={run};
