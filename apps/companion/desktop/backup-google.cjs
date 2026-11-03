'use strict';
// Publisher-owned OAuth configuration, never renderer-supplied endpoints.
const crypto=require('node:crypto');
const http=require('node:http');
const SCOPE='https://www.googleapis.com/auth/drive.file';
const TOKEN='https://oauth2.googleapis.com/token';
function configuration(value) {
 if(!value||typeof value.client_id!=='string'||!/^\d+-[a-zA-Z0-9_-]+\.apps\.googleusercontent\.com$/.test(value.client_id)||
    typeof value.client_secret!=='string'||value.client_secret.length>512||!value.client_secret)throw new Error('google.notConfigured');
 return {client_id:value.client_id,client_secret:value.client_secret};
}
async function boundedJSON(response,limit=1024*1024){
 let size=0;const chunks=[];
 try{for await(const chunk of response.body||[]){size+=chunk.length;if(size>limit)throw new Error('google.responseTooLarge');chunks.push(Buffer.from(chunk));}}
 catch{throw new Error('google.invalidResponse');}
 try{return JSON.parse(Buffer.concat(chunks).toString('utf8'));}catch{throw new Error('google.invalidResponse');}
}
function checkedTokens(value,previous=null){
 if(!value||typeof value.access_token!=='string'||!value.access_token||value.access_token.length>8192||value.token_type?.toLowerCase()!=='bearer'||
    !Number.isInteger(value.expires_in)||value.expires_in<1||value.expires_in>86400||
    (value.scope&&!value.scope.split(' ').includes(SCOPE)))throw new Error('google.invalidToken');
 const refresh=value.refresh_token||previous?.refresh_token;
 if(typeof refresh!=='string'||!refresh||refresh.length>8192)throw new Error('google.signInAgain');
 return {access_token:value.access_token,refresh_token:refresh,expires_at:Date.now()+value.expires_in*1000};
}
class GoogleOAuth {
 constructor({config,store,open,fetchImpl=fetch,timeout=180000}){this.config=configuration(config);this.store=store;this.open=open;this.fetch=fetchImpl;this.timeout=timeout;this.busy=false;}
 async exchange(values,signal){
  let response;
  try{response=await this.fetch(TOKEN,{method:'POST',redirect:'error',signal:AbortSignal.any([AbortSignal.timeout(30000),...(signal?[signal]:[])]),
    headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({...this.config,...values})});}
  catch{throw new Error(signal?.aborted?'google.cancelled':'google.network');}
  if(!response.ok){await response.body?.cancel();throw new Error(response.status===400?'google.signInAgain':'google.authorizationFailed');}
  return boundedJSON(response,32768);
 }
 async signIn({signal}={}){
  if(this.busy)throw new Error('google.busy');this.busy=true;
  const state=crypto.randomBytes(32).toString('base64url'),verifier=crypto.randomBytes(48).toString('base64url');
  let server,timer,onAbort,settle;
  try{
   if(signal?.aborted)throw new Error('google.cancelled');
   const callback=new Promise((resolve,reject)=>{settle={resolve,reject};});
   // Attach a rejection handler immediately, including while the browser opens.
   callback.catch(()=>{});
   server=http.createServer({maxHeaderSize:8192},(request,response)=>{
    const finish=(status,text)=>{response.writeHead(status,{'Content-Type':'text/plain; charset=utf-8','Cache-Control':'no-store','Content-Security-Policy':"default-src 'none'",'Connection':'close'});response.end(text);};
    const origin=`http://127.0.0.1:${server.address().port}`;
    let url;try{url=new URL(request.url,origin);}catch{finish(400,'Invalid request.');return;}
    if(request.method!=='GET'||request.headers.host!==origin.slice(7)||url.origin!==origin||url.pathname!=='/oauth2callback'||
       url.searchParams.getAll('state').length!==1||url.searchParams.get('state')!==state){finish(400,'Invalid authorization response.');return;}
    if(url.searchParams.has('error')){finish(400,'Authorization was not granted. Return to Daemons.');settle.reject(new Error('google.denied'));return;}
    const code=url.searchParams.get('code');
    if(!code||code.length>4096||url.searchParams.getAll('code').length!==1){finish(400,'Invalid authorization response.');return;}
    finish(200,'Authorization received. You can return to Daemons.');settle.resolve({code,redirect_uri:origin+'/oauth2callback'});
   });
   server.requestTimeout=5000;server.headersTimeout=5000;server.maxConnections=8;
   await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
   timer=setTimeout(()=>settle.reject(new Error('google.timeout')),this.timeout);
   onAbort=()=>settle.reject(new Error('google.cancelled'));signal?.addEventListener('abort',onAbort,{once:true});
   const url=new URL('https://accounts.google.com/o/oauth2/v2/auth');
   url.search=new URLSearchParams({client_id:this.config.client_id,response_type:'code',scope:SCOPE,
    redirect_uri:`http://127.0.0.1:${server.address().port}/oauth2callback`,state,
    code_challenge:crypto.createHash('sha256').update(verifier).digest('base64url'),code_challenge_method:'S256',access_type:'offline',prompt:'consent select_account'});
   await Promise.race([this.open(url.href),callback.then(()=>{})]);
   const answer=await callback;
   const value=checkedTokens(await this.exchange({...answer,code_verifier:verifier,grant_type:'authorization_code'},signal));
   await this.store.put({...value,client_id:this.config.client_id});return {connected:true};
  }finally{
   clearTimeout(timer);if(onAbort)signal?.removeEventListener('abort',onAbort);
   if(server){server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
   this.busy=false;
  }
 }
 async token({refresh=false,signal}={}){
  const saved=await this.store.get();
  if(!saved||saved.client_id!==this.config.client_id||typeof saved.refresh_token!=='string')throw new Error('google.signInAgain');
  if(!refresh&&typeof saved.access_token==='string'&&saved.expires_at>Date.now()+60000)return saved.access_token;
  const value=checkedTokens(await this.exchange({grant_type:'refresh_token',refresh_token:saved.refresh_token},signal),saved);
  await this.store.put({...value,client_id:this.config.client_id});return value.access_token;
 }
 async disconnect(){await this.store.delete();return {connected:false};}
}
module.exports={GoogleOAuth,configuration,boundedJSON,SCOPE};
