'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {HostAddition}=require('./host-addition.cjs');
function fixture(){
 const source={id:'source',host:'hermes',profile:'/owned/source/profile',state:'running',daemon:{name:'Aster',runtime:'/owned/runtime'}};
 const inventory={instances:[source]};
 const instances={base:'/owned',platform:'darwin',refresh:async()=>inventory};
 return {flow:new HostAddition(instances),instances,source};
}
test('an opaque daemon id becomes the owned profile, base and name',async()=>{
 const f=fixture();
 assert.deepEqual(await f.flow.request('source'),{source_profile:'/owned/source/profile',base:'/owned',name:'Aster'});
});
test('unknown, foreign, problematic or non-Hermes daemons are refused',async()=>{
 const f=fixture();
 await assert.rejects(f.flow.request('/owned/source/profile'),/attachment.source/);
 f.source.profile='/foreign/profile';await assert.rejects(f.flow.request('source'),/ownership/);
 f.source.profile='/owned/source/profile';f.source.host='openclaw';await assert.rejects(f.flow.request('source'),/ownership/);
 f.source.host='hermes';f.source.problem='ambiguous';await assert.rejects(f.flow.request('source'),/attachment.source/);
});
test('unsupported platforms cannot start a host addition',async()=>{
 const f=fixture();f.instances.platform='linux';
 await assert.rejects(f.flow.request('source'),/ownership/);
});
