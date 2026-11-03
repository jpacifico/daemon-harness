// Adapt the pinned host's native provider to Companion; never print credentials.
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
export async function connect({root,profile,emit,signal,login=true,forceLogin=false}) {
 signal.throwIfAborted();
 const progress=message=>{const stage={'Starting device code flow…':'starting','Requesting device code…':'starting','Waiting for device authorization…':'waiting','Exchanging device code…':'exchanging'}[message];if(stage)emit({stage});};
 emit({stage:'starting'});
 const load=relative=>import(pathToFileURL(path.join(root,relative)).href);
 const {buildOpenAIProvider}=await load('dist/extensions/openai/api.js');
 const sdk=await load('dist/plugin-sdk/provider-auth.js');
 const agentDir=path.join(profile,'agents/main/agent');
 const provider=buildOpenAIProvider();
 const stored=sdk.ensureAuthProfileStoreForLocalUpdate(agentDir);
 const candidates=Object.keys(stored.profiles).filter(id=>stored.profiles[id].provider==='openai'&&stored.profiles[id].type==='oauth');
 let profileId=forceLogin?undefined:(stored.order?.openai||[]).find(id=>candidates.includes(id))||candidates[0];
 if(!profileId){
  if(!login)throw Error('missing');
  const result=await provider.auth.find(m=>m.id==='device-code').run({
   config:{},agentDir,env:process.env,signal,isRemote:false,
   runtime:{log(){},error(){}},
   openUrl:async url=>{if(url!=='https://auth.openai.com/codex/device')throw Error('origin');emit({url});},
   prompter:{progress:message=>{progress(message);return {update:progress,stop(){}};},note:async()=>{},deviceCode:async({code})=>{
    if(!/^[A-Z0-9-]{4,32}$/.test(code))throw Error('code');emit({code});
   }}
  });
  if(signal.aborted||result.profiles.length!==1)throw Error('cancelled');
  const entry=result.profiles[0];
  if(entry.credential.provider!=='openai'||entry.credential.type!=='oauth')throw Error('credential');
  if(!await sdk.upsertAuthProfileWithLock({...entry,agentDir,stateDir:profile}))throw Error('storage');
  profileId=entry.profileId;
  if(!await sdk.updateAuthProfileStoreWithLock({agentDir,stateDir:profile,updater:store=>{store.order={...store.order,openai:[profileId]};return true;}}))throw Error('storage');
 }
 emit({stage:'authenticated'});
 emit({stage:'catalog'});
 const catalog=await provider.catalog.run({config:{},agentDir,env:process.env,
  resolveProviderAuth:()=>({mode:'oauth',profileId}),resolveProviderApiKey:()=>({})});
 // The native catalogue may fall back to static rows. Never call those account availability.
 if(!catalog?.outcomes?.some(o=>o.provider==='openai'&&o.status==='ready')){
  if(login&&!forceLogin&&catalog?.outcomes?.some(o=>o.status==='auth-rejected'))return connect({root,profile,emit,signal,login,forceLogin:true});
  throw Error('catalog');
 }
 const available=new Set(catalog.providers.openai.models.map(m=>m.id));
 const approved=['gpt-6-astra','gpt-6.1-sol'];
 const models=approved.filter(id=>available.has(id));
 // Public provider API returns already-filtered rows, not the raw upstream response.
 // Only fixed model identifiers/counts cross this bridge, never arbitrary metadata.
 emit({catalog:{status:'ready',raw_catalog_observed:false,native_count:Math.min(available.size,10000),excluded_count:Math.min(available.size-models.length,10000),models:approved.map(id=>({id,status:available.has(id)?'offered':'absent-native'}))}});
 if(!models.length)throw Error('catalog');
 signal.throwIfAborted();
 return {models};
}
if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){
 const [root,profile,mode]=process.argv.slice(2);
 const controller=new AbortController();
 const timer=setTimeout(()=>{controller.abort();process.exit(1);},900000);
 // A closed app/parent cancels this native login, including device polling.
 process.stdin.resume();process.stdin.on('end',()=>{controller.abort();process.exit(1);});
 const emit=value=>process.stdout.write(JSON.stringify(value)+'\n');
 // No native diagnostics or token-bearing exception text crosses the bridge.
 console.log=()=>{};console.warn=()=>{};console.error=()=>{};
 try{
  if(!path.isAbsolute(root)||!path.isAbsolute(profile)||!['login','catalog'].includes(mode))throw Error('arguments');
  for(const file of [root,profile,path.join(profile,'agents'),path.join(profile,'agents/main'),path.join(profile,'agents/main/agent'),path.join(profile,'agents/main/agent/auth-profiles.json'),path.join(profile,'agents/main/agent/auth.json')]){
   if(fs.existsSync(file)&&fs.realpathSync(file)!==path.resolve(file))throw Error('link');
  }
  emit(await connect({root,profile,emit,signal:controller.signal,login:mode==='login'}));
  // Pipes are asynchronous on Windows. Flush every queued frame before exiting.
  await new Promise((resolve,reject)=>process.stdout.write('',error=>error?reject(error):resolve()));
  clearTimeout(timer);process.exit(0);
 }catch{await new Promise(resolve=>process.stdout.write('',()=>resolve()));clearTimeout(timer);process.exit(1);}
}
