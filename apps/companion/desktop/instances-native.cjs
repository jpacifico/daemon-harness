'use strict';
// Opt-in qualification: only two uniquely named disposable user services.
const fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os'),crypto=require('node:crypto');
const {promisify}=require('node:util'),{execFile}=require('node:child_process');const exec=promisify(execFile);
const plist=require('plist');const {Instances,NativeServices}=require('./instances.cjs');
async function run({output,nodePath=process.execPath}={}) {
 const started=performance.now(),root=await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(),'daemons-instance-control-'))),home=path.join(root,'hôme');
 const namespace='com.daemonharness.instances-'+crypto.randomUUID().replaceAll('-','');
 const labels=['hermes','openclaw'].map(h=>namespace+'.'+h+'-gateway');
 const native=new NativeServices({home}),defs=[];
 const command=async(exe,args)=>exec(exe,args,{encoding:'utf8',windowsHide:true,timeout:20000,maxBuffer:2*1024*1024});
 const ps=script=>native.command('powershell.exe',['-NoLogo','-NoProfile','-NonInteractive','-Command',script]);
 const q=s=>"'"+s.replaceAll("'","''")+"'";
 const connect="$ErrorActionPreference='Stop';$s=New-Object -ComObject Schedule.Service;$s.Connect();";
 let result;
 try {
  await fs.mkdir(home,{recursive:true});
  const worker=path.join(root,'worker.cjs');await fs.writeFile(worker,"require('fs').writeFileSync('started',String(process.pid));setInterval(()=>{},1000);\n");
  for(const [i,host] of ['hermes','openclaw'].entries()){
   const profile=path.join(home,'.'+host);await fs.mkdir(profile);
   await fs.writeFile(path.join(profile,host==='hermes'?'config.yaml':'openclaw.json'),host==='hermes'?'model: synthetic\n':'{}');
   if(process.platform==='darwin'){
    const folder=path.join(home,'Library/LaunchAgents');await fs.mkdir(folder,{recursive:true});const file=path.join(folder,labels[i]+'.plist');
    const raw=plist.build({Label:labels[i],ProgramArguments:[nodePath,worker],WorkingDirectory:profile,RunAtLoad:true});
    await fs.writeFile(file,raw);defs.push({label:labels[i],file});await command('/bin/launchctl',['bootstrap',`gui/${process.getuid()}`,file]);
   }else if(process.platform==='win32'){
    defs.push({label:labels[i],folder:'\\DaemonHarness'});
    await ps(connect+"try{$f=$s.GetFolder('\\DaemonHarness')}catch{$f=$s.GetFolder('\\').CreateFolder('DaemonHarness')};$d=$s.NewTask(0);$d.Principal.UserId=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value;$d.Principal.LogonType=3;$d.Principal.RunLevel=0;$d.Settings.Enabled=$true;$d.Settings.ExecutionTimeLimit='PT0S';$a=$d.Actions.Create(0);$a.Path="+q(nodePath)+";$a.Arguments="+q('"'+worker+'"')+";$a.WorkingDirectory="+q(profile)+";$t=$f.RegisterTaskDefinition("+q(labels[i])+",$d,6,$null,$null,3,$null);$null=$t.Run($null)");
   }else throw new Error('Native test requires macOS or Windows');
  }
  const m=new Instances({home,appData:path.join(root,'app'),base:path.join(home,'DaemonHarness'),environment:{},services:native});await m.load();
  const diagnostic=async()=>{
   if(process.platform!=='win32')return null;
   const script=connect+"[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);$identity=[System.Security.Principal.WindowsIdentity]::GetCurrent();$checks=@();"+labels.map(label=>"$t=$s.GetFolder('\\DaemonHarness').GetTask("+q(label)+");$d=$t.Definition;$checks+=@{sidMatches=($d.Principal.UserId -eq $identity.User.Value);nameMatches=($d.Principal.UserId -eq $identity.Name);shortNameMatches=($d.Principal.UserId -eq $env:USERNAME);runLevel=[int]$d.Principal.RunLevel;state=[int]$t.State;actionCount=[int]$d.Actions.Count;actionType=[int]$d.Actions.Item(1).Type};").join('')+"ConvertTo-Json -InputObject @($checks) -Compress";
   try{return JSON.parse((await ps(script)).stdout);}catch{return 'fixture inspection unavailable';}
  };
  const wait=async(predicate,phase='observation')=>{let last;for(let i=0;i<80;i++){last=await m.refresh();if(predicate(last.instances))return last;await new Promise(r=>setTimeout(r,250));}throw new Error('Native state did not converge: '+phase+' '+JSON.stringify({available:last?.servicesAvailable,profiles:last?.instances.map(x=>({host:x.host,state:x.state,canConnect:x.canConnect})),native:await diagnostic()}));};
  const initial=await wait(rows=>rows.length===2&&rows.every(r=>r.state==='running'),'initial services');
  const hermes=initial.instances.find(r=>r.host==='hermes');
  await m.change(hermes.id,'connect',async()=>true);
  await m.change(hermes.id,'stop',async()=>true);
  await wait(rows=>rows.find(r=>r.host==='hermes').state==='stopped'&&rows.find(r=>r.host==='openclaw').state==='running');
  await m.change(hermes.id,'start',async()=>true);await wait(rows=>rows.every(r=>r.state==='running'));
  const pidFile=path.join(home,'.hermes/started');let before=await fs.readFile(pidFile,'utf8');
  await m.change(hermes.id,'restart',async()=>true);await wait(rows=>rows.every(r=>r.state==='running'));
  for(let i=0;i<40&&await fs.readFile(pidFile,'utf8')===before;i++)await new Promise(r=>setTimeout(r,100));
  if(await fs.readFile(pidFile,'utf8')===before)throw new Error('Restart did not replace the process');
  result={schema:'daemon.companion.instances-native.v1',status:'passed',platform:process.platform,elapsedMs:Math.round(performance.now()-started),start_stop_restart:true,sibling_untouched:true,personal_services_changed:0,services_started:2,cleanup_verified:false};
  return result;
 } finally {
  // Remove only this test's unpredictable exact service names, then its files.
  for(const d of defs){
   if(process.platform==='darwin'){try{await command('/bin/launchctl',['bootout',`gui/${process.getuid()}/${d.label}`]);}catch{}}
   else if(process.platform==='win32')await ps(connect+"$f=$s.GetFolder('\\DaemonHarness');$t=$f.GetTask("+q(d.label)+");$t.Stop(0);$f.DeleteTask("+q(d.label)+",0)");
  }
  const cleanup=await native.list();
  if(!cleanup.available)throw new Error('Test service cleanup could not be verified; preserve test directory '+root);
  const remaining=cleanup.items.filter(s=>labels.includes(s.label)&&s.state==='running');
  if(remaining.length)throw new Error('Test service cleanup did not complete; preserve test directory '+root);
  await fs.rm(root,{recursive:true,force:true});
  if(result){result.cleanup_verified=true;if(output)await fs.writeFile(output,JSON.stringify(result,null,2)+'\n');}
 }
}
module.exports={run};
if(require.main===module)run({output:process.argv[2]}).then(r=>console.log(JSON.stringify(r))).catch(e=>{console.error(e.message);process.exitCode=1;});
