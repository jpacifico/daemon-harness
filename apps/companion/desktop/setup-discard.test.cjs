const test=require('node:test');
const assert=require('node:assert/strict');
const {context,supported}=require('./setup-discard.cjs');
const root='C:\\Users\\Test\\Companion\\instance-ABC123';
function fixture(){return {platform:'win32',backend:{setupHomes:{root:'C:\\Users\\Test\\Companion',resolve:async id=>{assert.equal(id,'opaque');return root;},installed:async()=>false}},instances:{platform:'win32',refresh:async()=>({instances:[]}),services:{list:async()=>({available:true,items:[]})}}};}
test('Windows review resolves the opaque setup without launchd paths',async()=>{
  assert(supported('win32'));assert(supported('darwin'));assert(!supported('linux'));
  assert.deepEqual(await context(fixture(),'opaque'),{root,base:'C:\\Users\\Test\\Companion',launch_agents:undefined});
});
test('active and complete installations cannot be discarded',async()=>{
  let f=fixture();f.backend.active=true;await assert.rejects(context(f,'opaque'),/running/);
  f=fixture();f.backend.setupHomes.installed=async()=>true;await assert.rejects(context(f,'opaque'),/completed/);
});
test('Windows task references compare case and extended prefix',async()=>{
  const f=fixture(),id='a'.repeat(32),cwd='\\\\?\\'+root.toUpperCase()+'\\hosts\\hermes\\installations\\'+id;
  f.instances.services.list=async()=>({available:true,items:[{cwd,args:[],label:`com.daemonharness.test.${id}.hermes-gateway`}]});
  await context(f,'opaque');
  f.instances.services.list=async()=>({available:true,items:[{cwd,args:[],label:'foreign-task'}]});
  await assert.rejects(context(f,'opaque'),/shared/);
});
test('shared source and unavailable inventory block cleanup',async()=>{
  const f=fixture();f.instances.refresh=async()=>({instances:[{profile:'C:\\Other\\profile',daemon:{source:root.toLowerCase()+'\\source'}}]});
  await assert.rejects(context(f,'opaque'),/shared/);
  f.instances.services.list=async()=>({available:false,items:[]});
  await assert.rejects(context(f,'opaque'),/services/);
});
