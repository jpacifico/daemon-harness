'use strict';
const fs=require('node:fs/promises'), path=require('node:path');
async function fixture(root) {
  const home=path.join(root,'home'),appData=path.join(root,'app'),base=path.join(home,'DaemonHarness');
  const memory=path.join(home,'memory'),hermes=path.join(home,'.hermes'),openclaw=path.join(home,'.openclaw');
  const write=async(file,data)=>{await fs.mkdir(path.dirname(file),{recursive:true});await fs.writeFile(file,data);};
  await write(path.join(hermes,'config.yaml'),'model: synthetic\nsecret: never-export-this-token\n');
  await write(path.join(hermes,'.env'),'DAEMON_CORE_MEMORY_ROOT="'+path.join(memory,'.daemon')+'"\nSECRET=never-export-this-token\n');
  await fs.mkdir(path.join(hermes,'plugins/daemon_core'),{recursive:true});
  await write(path.join(memory,'.daemon/core/daemon_identity.json'),JSON.stringify({name:'Aster',gender:'feminine',user:{preferred_name:'Morgan',pronouns:'they/them',user_id:'12345',platform:'telegram',source:'companion-onboarding'}}));
  await write(path.join(memory,'Areas/exact.md'),'Synthetic fact: preserve exactly.\n');
  await write(path.join(openclaw,'openclaw.json'),JSON.stringify({plugins:{slots:{contextEngine:'daemon-harness'},entries:{'daemon-harness':{enabled:true,config:{daemonUrl:'http://127.0.0.1:8761'}}}},credentials:{token:'never-export-this-token'}}));
  await write(path.join(home,'.hermes/profiles/plain/config.yaml'),'model: synthetic\n');
  const attempt=path.join(base,'Companion/private/hosts/hermes/installations','b'.repeat(32));
  await write(path.join(attempt,'profile/config.yaml'),'model: synthetic\n');
  const services={available:true,calls:[],items:[
    {label:'ai.hermes.gateway',digest:'a'.repeat(64),cwd:hermes,args:['/synthetic/hermes'],env:{HERMES_HOME:hermes},state:'running'},
    {label:'ai.openclaw.gateway',digest:'b'.repeat(64),cwd:openclaw,args:['/synthetic/openclaw'],env:{},state:'stopped'},
    {label:'com.daemonharness.openclaw-api',digest:'c'.repeat(64),cwd:home,args:['/synthetic/api','--port','8761'],env:{DAEMON_CORE_MEMORY_ROOT:path.join(memory,'.daemon')},state:'running'},
    {label:'com.daemonharness.test.'+'b'.repeat(32)+'.hermes-gateway',digest:'d'.repeat(64),cwd:attempt,args:['/synthetic/hermes'],env:{},state:'running'}
  ],async list(){return {available:this.available,items:this.items.map(x=>({...x}))};},async control(s,action){this.calls.push({label:s.label,action});this.items.find(x=>x.label===s.label).state=action==='stop'?'stopped':'running';}};
  return {home,appData,base,services,memory,hermes,openclaw,write};
}
module.exports={fixture};
