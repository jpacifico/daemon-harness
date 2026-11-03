'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os'),crypto=require('node:crypto'),{EventEmitter}=require('node:events');
const {HostInterface,localUrl}=require('./host-interface.cjs');
const {createLocalizer}=require('./i18n.js'),catalog=require('./locales/messages.json');
test('only known successful confirmations are emphasized',()=>{
 const locale=createLocalizer(catalog);
 for(const key of ['api.provider.10','api.provider.17','telegram.accepted','chatgpt.done']){
  assert(locale.isConfirmation(catalog[key].en.replace('{bot}','synthetic_bot')));
  for(const language of ['en','fr'])assert(locale.message(catalog[key].en.replace('{bot}','synthetic_bot'),language));
 }
 for(const value of ['API key not accepted','<script>accepted</script>',catalog['setup.unconfirmed'].en])assert(!locale.isConfirmation(value));
});
async function fixture(t,host='hermes'){
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'host-ui-test-'));
 const assets=path.join(root,'assets');await fs.mkdir(assets);await fs.writeFile(path.join(assets,'index.html'),'synthetic');
 await fs.writeFile(path.join(assets,'companion-assets.json'),JSON.stringify({revision:'pinned',files:{'index.html':crypto.createHash('sha256').update('synthetic').digest('hex')}}));
 const row={id:'id',profile:path.join(root,'profile'),host,state:'running'},calls=[],opened=[],children=[];
 const manager=new HostInterface({assets,stateRoot:path.join(root,'state'),timeout:1000,
  instances:{base:root,refresh:async()=>{},rows:new Map([['id',row]])},
  run:async request=>{calls.push(request);return {host,profile:row.profile,revision:'pinned',executable:'/synthetic/python',cwd:root,environment:{HERMES_HOME:row.profile,SYNTHETIC_KEY:'not-for-renderer'},url:'http://127.0.0.1:12345/#token=not-for-renderer'};},
  open:async url=>opened.push(url),probe:async()=>({ok:true}),
  spawnProcess:(exe,args,options)=>{calls.push({exe,args,options});const c=new EventEmitter();c.exitCode=null;c.signalCode=null;c.pid=12345;c.kill=()=>{c.exitCode=0;c.emit('exit');};children.push(c);fs.writeFile(options.env.HERMES_DESKTOP_READY_FILE,JSON.stringify({port:12345})).catch(()=>{});return c;}});
 t.after(async()=>{await manager.close();await fs.rm(root,{recursive:true,force:true});});return {manager,row,calls,opened,children,assets};
}
test('Hermes isolates the profile, hides secrets, reuses and closes its own child',async t=>{
 const f=await fixture(t);assert.deepEqual(await f.manager.launch('id'),{opened:true});
 const call=f.calls[1];assert(call.args.includes('--isolated'));assert(call.args.includes('--skip-build'));assert.equal(call.options.env.HERMES_HOME,f.row.profile);
 assert(!JSON.stringify(f.opened).includes('not-for-renderer'));
 await f.manager.launch('id');assert.equal(f.children.length,1);assert.equal(f.opened.length,2);
 await f.manager.close();assert.equal(f.children[0].exitCode,0);
});
test('asset mismatch and foreign inventory id fail without launching',async t=>{
 const f=await fixture(t);await assert.rejects(f.manager.launch('../../other'),/unavailable/);
 await fs.writeFile(path.join(f.assets,'index.html'),'changed');await assert.rejects(f.manager.launch('id'),/assets/);assert.equal(f.children.length,0);
});
test('OpenClaw opens the exact loopback interface without starting a gateway',async t=>{
 const f=await fixture(t,'openclaw');assert.deepEqual(await f.manager.launch('id'),{opened:true});assert.equal(f.children.length,0);assert.equal(f.opened[0],'http://127.0.0.1:12345/#token=not-for-renderer');
 f.row.state='stopped';await assert.rejects(f.manager.launch('id'),/stopped/);
});
test('browser failure stops the newly launched dashboard',async t=>{
 const f=await fixture(t);f.manager.open=async()=>{throw new Error('browser refused');};await assert.rejects(f.manager.launch('id'),/browser refused/);assert.equal(f.children[0].exitCode,0);assert.equal(f.manager.sessions.size,0);
});
test('startup timeout stops its child and never opens the browser',async t=>{
 const f=await fixture(t);f.manager.timeout=0;await assert.rejects(f.manager.launch('id'),/timeout/);assert.equal(f.opened.length,0);assert.equal(f.children[0].exitCode,0);
});
test('external and credential-bearing URLs refused',()=>{
 for(const url of ['https://example.com','file:///etc/passwd','http://localhost:12345/','http://127.0.0.1:80/','http://name:password@127.0.0.1:12345/'])assert.throws(()=>localUrl(url));
});
