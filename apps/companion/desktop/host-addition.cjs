'use strict';
// Main-process coordinator: renderer values are opaque inventory IDs, never paths.
// It only resolves the owned daemon; the guided session does the rest.
const path=require('node:path');
const within=(parent,child)=>child===parent||child.startsWith(parent+path.sep);
class HostAddition {
 constructor(instances){this.instances=instances;}
 supported(){return ['darwin','win32'].includes(this.instances.platform);}
 async context(id){
  const inventory=await this.instances.refresh();
  const source=inventory.instances.find(r=>r.id===id);
  if(!source?.daemon?.runtime||source.problem)throw new Error('attachment.source');
  const eligible=source.host==='hermes'&&within(this.instances.base,source.profile);
  return {inventory,source,eligible};
 }
 async request(id){
  const c=await this.context(id);
  if(!c.eligible||!this.supported())throw new Error('attachment.ownership');
  return {source_profile:c.source.profile,base:this.instances.base,name:c.source.daemon.name};
 }
}
module.exports={HostAddition};
