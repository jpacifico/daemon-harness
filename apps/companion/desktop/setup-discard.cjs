'use strict';
const paths=require('node:path');
const supported=platform=>['darwin','win32'].includes(platform);
// Normalize only for dependency comparison, never to grant ownership or delete.
const normal=(value,platform)=>platform==='win32'?String(value).replaceAll('\\\\?\\','').replaceAll('/','\\').toLowerCase():String(value);
async function context({platform,backend,instances},id) {
  const path=platform==='win32'?paths.win32:paths.posix;
  if(!supported(platform))throw new Error('discard.platform');
  if(backend.active||backend.settingsBusy)throw new Error('discard.running');
  const root=await backend.setupHomes.resolve(id);
  if(await backend.setupHomes.installed(root))throw new Error('discard.completed');
  const inventory=await instances.refresh();
  const native=await instances.services.list();
  if(!native.available)throw new Error('discard.services');
  for(const service of native.items) {
    const references=normal([service.cwd,...service.args,...Object.values(service.env||{})].join('\n'),platform);
    if(references.includes(normal(root,platform))) {
      const match=/^com\.daemonharness\.test\.([a-f0-9]{32})\.(hermes-gateway|openclaw-gateway|openclaw-api|after-turn-worker|dreaming|embeddings)$/.exec(service.label);
      if(!match || !['hermes','openclaw'].some(host=>references.includes(normal(path.join(root,'hosts',host,'installations',match[1]),platform))))throw new Error('discard.shared');
    }
  }
  const within=(p,parent)=>typeof p==='string'&&(normal(p,platform)===normal(parent,platform)||normal(p,platform).startsWith(normal(parent,platform)+path.sep));
  for(const row of inventory.instances) {
    if(!within(row.profile,root)&&[row.daemon?.source,row.daemon?.memory].some(p=>within(p,root)))throw new Error('discard.shared');
  }
  return {root,base:backend.setupHomes.root,launch_agents:instances.platform==='darwin'?path.join(instances.home,'Library/LaunchAgents'):undefined};
}
module.exports={supported,context};
