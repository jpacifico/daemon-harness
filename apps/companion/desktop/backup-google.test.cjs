'use strict';
const {test}=require('node:test');const assert=require('node:assert/strict');const crypto=require('node:crypto');
const {GoogleOAuth,SCOPE,configuration}=require('./backup-google.cjs');
const config={client_id:'123-synthetic.apps.googleusercontent.com',client_secret:'public-desktop-test-value'};
function store(){let value=null;return {get:async()=>value,put:async v=>{value=v;},delete:async()=>{value=null;}};}
const tokens={access_token:'synthetic-access',refresh_token:'synthetic-refresh',expires_in:3600,token_type:'Bearer',scope:SCOPE};
test('PKCE loopback sign-in rejects foreign state and saves only validated tokens',async()=>{
 const storage=store();let challenge;
 const auth=new GoogleOAuth({config,store:storage,open:async address=>{
  const url=new URL(address);assert.equal(url.origin,'https://accounts.google.com');assert.equal(url.searchParams.get('scope'),SCOPE);
  challenge=url.searchParams.get('code_challenge');const callback=new URL(url.searchParams.get('redirect_uri'));assert.equal(callback.hostname,'127.0.0.1');
  callback.search=new URLSearchParams({state:'foreign',code:'synthetic-code'});assert.equal((await fetch(callback)).status,400);
  callback.search=new URLSearchParams({state:url.searchParams.get('state'),code:'synthetic-code'});assert.equal((await fetch(callback)).status,200);
 },fetchImpl:async(url,options)=>{assert.equal(url,'https://oauth2.googleapis.com/token');assert.equal(options.redirect,'error');
  assert.equal(crypto.createHash('sha256').update(options.body.get('code_verifier')).digest('base64url'),challenge);
  return Response.json(tokens);
 }});
 assert.deepEqual(await auth.signIn(),{connected:true});assert.equal(await auth.token(),'synthetic-access');await auth.disconnect();assert.equal(await storage.get(),null);
});
test('denial, timeout and cancellation never persist a credential',async()=>{
 for(const mode of ['denied','timeout','cancelled']){
  const storage=store(),controller=new AbortController();
  const auth=new GoogleOAuth({config,store:storage,timeout:30,open:async address=>{
   if(mode==='denied'){const u=new URL(address),c=new URL(u.searchParams.get('redirect_uri'));c.search=new URLSearchParams({state:u.searchParams.get('state'),error:'access_denied'});await fetch(c);}
   if(mode==='cancelled')controller.abort();
  },fetchImpl:()=>{throw new Error('must not exchange');}});
  await assert.rejects(auth.signIn({signal:controller.signal}),new RegExp(mode));assert.equal(await storage.get(),null);
 }
});
test('refresh retains refresh token and refuses invalid scope or malformed token',async()=>{
 const storage=store();await storage.put({...tokens,client_id:config.client_id,expires_at:0});let count=0;
 const auth=new GoogleOAuth({config,store:storage,open:()=>{},fetchImpl:async(_,options)=>{
  count++;assert.equal(options.body.get('grant_type'),'refresh_token');return Response.json({...tokens,refresh_token:undefined});
 }});
 assert.equal(await auth.token(),'synthetic-access');assert.equal(count,1);assert.equal((await storage.get()).refresh_token,'synthetic-refresh');
 auth.fetch=async()=>Response.json({...tokens,scope:'https://example.com'});await assert.rejects(auth.token({refresh:true}),/invalidToken/);
 auth.fetch=async()=>new Response('secret raw error',{status:400});await assert.rejects(auth.token({refresh:true}),/^Error: google.signInAgain$/);
 assert.throws(()=>configuration({client_id:'https://evil.example',client_secret:'x'}),/notConfigured/);
});

test('a browser opener that never returns is bounded by authorization timeout',async()=>{
 const auth=new GoogleOAuth({config,store:store(),timeout:20,open:()=>new Promise(()=>{}),fetchImpl:()=>{throw new Error('must not exchange');}});
 await assert.rejects(auth.signIn(),/google.timeout/);assert.equal(auth.busy,false);
});
