'use strict';
// Test-only entry, never selected by renderer IPC. Uses the real Python duplex
// transport and Telegram retry loop, with synthetic external boundaries.
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
async function scenario(window, backend, language) {
  const started = performance.now();
  const output = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'daemons-companion-connected-')));
  const locale = require('./i18n.js').createLocalizer(require('./locales/messages.json'));
  const evaluate = async code => {
    if (language === 'fr') code = code.replace(/textContent.includes\('([^']+)'\)/g, (_match, text) => 'textContent.includes(' + JSON.stringify(locale.message(text, language)) + ')');
    try { return await window.webContents.executeJavaScript(code); }
    catch (error) { throw new Error(code + ': ' + error.message); }
  };
  const wait = async expression => {
    const end = Date.now() + 15000;
    while (!await evaluate(expression)) {
      if (Date.now() > end) throw new Error('UI wait failed: ' + expression);
      await new Promise(resolve => setTimeout(resolve, 60));
    }
  };
  const capture = async name => {
    await evaluate("document.getAnimations().filter(a=>a.effect?.getTiming().iterations!==Infinity).forEach(a=>a.finish())");
    await evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
    await new Promise(resolve => setTimeout(resolve, 180));
    if (!await evaluate('document.documentElement.scrollWidth <= innerWidth')) throw new Error('Horizontal overflow');
    if (!await evaluate('document.querySelector(".footer").getBoundingClientRect().bottom <= innerHeight + 1')) throw new Error('Navigation buttons below window');
    await layout(window);
    await contrast(window);
    await fs.writeFile(path.join(output, name + '.png'), (await window.webContents.capturePage()).toPNG());
  };
  const assert = async (expression, message) => { if (!await evaluate(expression)) throw new Error(message); };
  backend.setupHomes = new (require('./setup-homes.cjs').SetupHomes)(output);
  backend.installHome = path.join(output, 'private');
  backend.bootstrapHome = path.join(output, 'bootstrap');
  const runtime = await backend.prepare(new AbortController().signal);
  backend.smokeRuntime = runtime;
  const source = path.join(backend.packageRoot, 'payload', 'src');
  // Python cannot read Electron's ASAR archive. Copy this test-only fixture
  // into the disposable directory; the production engine is in extraResources.
  const fixture = path.join(output, 'transport-fixture.py');
  await fs.writeFile(fixture, await fs.readFile(path.join(__dirname, 'smoke-fixture.py')));
  backend.installHome = output;
  backend.prepare = async () => ({ python: runtime.python, env: runtime.env,
    args: ['-I', '-B', '-u', '-c', 'import sys,runpy;sys.path.insert(0,sys.argv.pop(1));sys.path.insert(1,sys.argv.pop(1));p=sys.argv.pop(1);runpy.run_path(p,run_name="__main__")', source, runtime.libraries, fixture, path.join(output, 'memory')] });
  await wait("!!document.querySelector('[data-action=settings]')");
  if (language === 'en') await assert("document.documentElement.lang==='en'", 'First launch must be English');
  await evaluate(`changeLanguage(${JSON.stringify(language)})`);
  await wait(`document.documentElement.lang===${JSON.stringify(language)}`);
  await window.webContents.reload();
  await wait(`!!document.querySelector('[data-action=settings]') && !state.instanceBusy && document.documentElement.lang===${JSON.stringify(language)}`);
  await capture('01-welcome');
  await evaluate("globalThis.savedAuthState={...state};Object.assign(state,{page:'setup',mode:'add-host',phase:'model',active:true,prompt:null,guidance:[],error:'',chatgptStage:'waiting',chatgptCode:'TEST-0000',links:['https://auth.openai.com/codex/device'],activity:'Connect ChatGPT for OpenClaw conversations...'});render()");
  await assert("document.body.textContent.includes('TEST-0000') && !!document.querySelector('[data-link]') && !!document.querySelector('#activity .spinner') && document.querySelectorAll('.steps span').length===5", 'Add a host shows the same ChatGPT code, browser action and progress, without the naming step');
  await capture('01-chatgpt-openclaw');
  await evaluate("globalThis.catalogFixture={status:'ready',raw_catalog_observed:false,native_count:2,excluded_count:1,models:['gpt-6-astra','gpt-6.1-sol'].map((id,i)=>({id,status:i===1?'offered':'absent-native'}))}");
  await evaluate("Object.assign(state,{page:'setup',phase:'model',active:true,prompt:null,guidance:['ChatGPT sign-in could not be completed. You can retry or choose another provider.'],error:'stale error'});render()");
  window.webContents.send('setup-event',{type:'connection-start'});
  window.webContents.send('setup-event',{type:'chatgpt-stage',stage:'authenticated'});
  await wait("state.chatgptStage==='authenticated'");
  await assert("state.guidance.length===0 && state.error==='' && !document.querySelector('.guidance')", 'Retry must clear prior failure before authenticated stage');
  window.webContents.send('setup-event',{type:'link',url:'https://auth.openai.com/oauth/authorize?state=synthetic'});
  await wait("state.links.some(url=>url.includes('auth.openai.com'))");
  window.webContents.send('setup-event',{type:'prompt',id:'synthetic-after-sign-in',kind:'choice',title:'Choose your model',options:['Astra'],default:0});
  await wait("state.prompt?.id==='synthetic-after-sign-in'");
  await assert("!state.links.some(url=>url.includes('auth.openai.com')) && !document.body.textContent.includes('Continue current ChatGPT connection') && !document.body.textContent.includes('Poursuivre la connexion ChatGPT')", 'A finished ChatGPT sign-in must not keep its browser link');
  // Hermes checks both sign-ins and loads its models before the next question: the link goes when its sign-in ends.
  await evaluate("Object.assign(state,{prompt:null,activity:'Complete the ChatGPT connection in your browser…'});render()");
  window.webContents.send('setup-event',{type:'link',url:'https://auth.openai.com/oauth/authorize?state=synthetic-hermes'});
  await wait("!!document.querySelector('[data-link]')");
  window.webContents.send('setup-event',{type:'sign-in-ended'});
  await wait("!state.links.some(url=>url.includes('auth.openai.com'))");
  await assert("!document.querySelector('[data-link]') && !document.body.textContent.includes('Continue current ChatGPT connection') && !document.body.textContent.includes('Poursuivre la connexion ChatGPT')", 'An ended ChatGPT sign-in drops its browser link before the next question');
  window.webContents.send('setup-event',{type:'model-catalog',catalog:await evaluate('globalThis.catalogFixture')});
  window.webContents.send('setup-event',{type:'prompt',id:999,kind:'choice',title:'Choose your model',default:0,options:['GPT-6.1-Sol','Refresh model list'],guidance:['ChatGPT connection verified.']});
  await wait("state.prompt?.id===999");
  await assert("document.querySelector('h1').textContent.includes('Choose your model.') && document.querySelector('[data-action=submit]') && !document.querySelector('.model-catalog').open", 'One model is a choice, not a failed connection; technical exclusions stay collapsed');
  await capture('03b-chatgpt-first-model-choice');

  await evaluate("Object.assign(state,{page:'setup',phase:'model',active:true,prompt:null,activity:'ChatGPT connected. Loading the models reported by OpenClaw...',modelCatalog:globalThis.catalogFixture,chatgptCode:'',links:[]});render()");
  await assert("!!document.querySelector('.model-catalog') && !!document.querySelector('#activity .spinner')", 'First setup must expose the same model evidence');

  await evaluate("Object.assign(state,globalThis.savedAuthState);delete globalThis.savedAuthState;render()");

  await evaluate("globalThis.savedFeedbackState={...state};Object.assign(state,{page:'setup',phase:'model',active:true,prompt:null,activity:'Checking private model access...',guidance:['API key accepted. Choose your model next.']});render()");
  await assert("!!document.querySelector('.guidance strong') && !document.querySelector('.installation-progress') && !!document.querySelector('#activity .spinner') && !!document.querySelector('[data-activity-elapsed]')", 'Confirmed step or honest activity feedback missing');
  await capture('01a-confirmed-activity');
  await evaluate("Object.assign(state,globalThis.savedFeedbackState);delete globalThis.savedFeedbackState;render()");
  await assert("document.querySelector('nav .nav').dataset.action==='instances' && document.querySelector('nav [aria-current=page]').dataset.action==='instances' && document.querySelectorAll('nav .nav').length===2 && !!document.querySelector('aside > .settings-button[data-action=settings]') && document.querySelectorAll('[data-language]').length===0 && !document.querySelector('.titlebar,[data-theme-toggle]')", 'First launch navigation is not My daemons');
  await assert("!document.querySelector('[data-action=start]') && document.querySelectorAll('.footer .primary').length===1 && !!document.querySelector('[data-action=new-instance]')", 'Fresh setup has a misleading resume action');
  await assert("!document.body.textContent.match(/companion|compagnon|instance/i)", 'Old first-launch vocabulary');
  await evaluate("document.querySelector('[data-action=new-instance]').click()");
  await wait("!!document.querySelector('#answer')");
  await assert("document.querySelector('[data-action=submit]').disabled", 'Empty name accepted');
  await evaluate("document.querySelector('#answer').value='Aster'; document.querySelector('#answer').dispatchEvent(new Event('input'))");
  // Change the display while an unsubmitted accented name is being edited.
  await evaluate("document.querySelector('#answer').value='Élodie'; document.querySelector('#answer').dispatchEvent(new Event('input'))");
  const alternate = language === 'en' ? 'fr' : 'en';
  await evaluate(`changeLanguage(${JSON.stringify(alternate)})`);
  await wait(`document.documentElement.lang===${JSON.stringify(alternate)}`);
  await assert("document.querySelector('#answer').value==='Élodie'", 'Language switch lost the draft');
  await evaluate(`changeLanguage(${JSON.stringify(language)})`);
  await wait(`document.documentElement.lang===${JSON.stringify(language)}`);
  await evaluate("changeTheme('dark')");
  await wait("theme==='dark' && matchMedia('(prefers-color-scheme: dark)').matches");
  await assert("state.page==='setup' && document.querySelector('#answer').value==='Élodie'", 'Theme toggle left setup or lost the draft');
  await capture('01b-toggle-dark');
  await evaluate("document.querySelector('[data-action=about]').click()");
  await capture('01c-about-dark');
  await evaluate("document.querySelector('[data-action=setup]').click()");
  await assert("document.querySelector('#answer').value==='Élodie'", 'About navigation lost the draft');
  await evaluate("document.querySelector('#answer').value='Aster'; document.querySelector('#answer').dispatchEvent(new Event('input'))");
  window.webContents.sendInputEvent({type:'keyDown',keyCode:'Tab'});
  window.webContents.sendInputEvent({type:'keyUp',keyCode:'Tab'});
  await assert("document.activeElement.matches('button, select, input')", 'Keyboard focus lost');
  await capture('02-identity');
  await assert("document.querySelector('#answer').value === 'Aster'", 'Draft changed during capture');
  await evaluate("document.querySelector('[data-action=submit]').click()");
  await wait("state.prompt?.title==='Conversation language'");
  await assert("document.querySelector('h1').textContent.includes('Choose your conversation language.')", 'Language heading missing');
  await assert("!document.querySelector('#answer') && document.querySelectorAll('[name=answer]').length===2", 'First meeting must ask language, not a name');
  await evaluate("document.querySelectorAll('[name=answer]')[1].click()");
  await capture('02b-conversation-language');
  await evaluate("document.querySelector('[data-action=submit]').click()");
  await wait("state.prompt?.title==='3. Model access to prepare'");
  await assert("document.querySelectorAll('[name=answer]').length===3 && !document.body.textContent.includes('SpaceXSI') && !document.body.textContent.includes('Mistral')", 'Offered provider choices differ');
  await capture('03a-providers');
  await evaluate("document.querySelectorAll('[name=answer]')[1].click(); document.querySelector('[data-action=submit]').click()");
  await wait("state.prompt?.title==='Paste your API key (hidden)'");
  await assert("document.querySelector('#answer').type==='password'", 'API key entry is not masked');
  await capture('03b-api-key');
  await evaluate("document.querySelector('#answer').value='synthetic-wrong-key'; document.querySelector('#answer').dispatchEvent(new Event('input')); document.querySelector('[data-action=submit]').click()");
  await wait("state.prompt?.title==='API connection not ready'");
  await assert("!document.body.textContent.includes('synthetic-wrong-key')", 'Rejected API key leaked');
  await capture('03c-api-retry');
  await evaluate("document.querySelector('[data-action=submit]').click()");
  await wait("state.prompt?.title==='Paste your API key (hidden)'");
  await assert("document.querySelector('#answer').value===''", 'Rejected key retained');
  await evaluate("document.querySelector('#answer').value='synthetic-valid-api-key'; document.querySelector('#answer').dispatchEvent(new Event('input')); document.querySelector('[data-action=submit]').click()");
  await wait("state.prompt?.title==='Choose your model'");
  await assert("[...document.querySelectorAll('[name=answer]')].length===1 && /Astra/.test(document.querySelector('form')?.textContent||document.body.textContent) && /Recommended|Recommandé/.test(document.body.textContent)", 'Astra recommendation missing');
  await capture('03-model');
  await evaluate("document.querySelector('[data-action=submit]').click()");
  await wait("document.body.textContent.includes('Connect Telegram Web')");
  await assert("!document.querySelector('.guidance')?.textContent.includes('Synthetic identity')", 'Old screen guidance leaked');
  await capture('03b-telegram-web');
  await evaluate("document.querySelector('[data-action=submit]').click()");
  await wait("state.prompt?.options?.includes('I am signed in — continue')");
  if (backend.smokeOpenedLinks.join(',') !== 'https://web.telegram.org/') throw new Error('Continue did not open Telegram Web once');
  await evaluate("document.querySelector('[data-action=submit]').click()");
  await wait("!!document.querySelector('input[type=password]')");
  await evaluate("document.querySelector('#answer').value='draft-secret'; document.querySelector('#answer').dispatchEvent(new Event('input'))");
  await evaluate(`changeLanguage(${JSON.stringify(alternate)})`);
  await wait(`document.documentElement.lang===${JSON.stringify(alternate)}`);
  await assert("document.querySelector('#answer').value==='draft-secret' && !document.body.textContent.includes('draft-secret')", 'Secret draft lost or exposed');
  await evaluate(`changeLanguage(${JSON.stringify(language)})`);
  await wait(`document.documentElement.lang===${JSON.stringify(language)}`);
  await evaluate("changeTheme('dark')");
  await wait("theme==='dark' && matchMedia('(prefers-color-scheme: dark)').matches");
  await evaluate("document.querySelector('[data-action=about]').click(); document.querySelector('[data-action=setup]').click()");
  await assert("document.querySelector('#answer').value==='draft-secret' && document.querySelector('#answer').type==='password' && !document.body.textContent.includes('draft-secret')", 'Appearance navigation lost or exposed secret');
  await evaluate("document.querySelector('#answer').value=''");
  await capture('04-telegram');
  await evaluate("document.querySelector('#answer').value='wrong-token'; document.querySelector('#answer').dispatchEvent(new Event('input')); document.querySelector('[data-action=submit]').click()");
  await wait("document.body.textContent.includes('Telegram verification did not finish')");
  await assert("!document.body.textContent.includes('wrong-token')", 'Token leaked');
  await capture('05-token-retry');
  await evaluate("document.querySelector('[data-action=submit]').click()");
  await wait("!!document.querySelector('input[type=password]')");
  await assert("document.querySelector('#answer').value === ''", 'Secret reused');
  await evaluate("document.querySelector('#answer').value='synthetic-valid-token'; document.querySelector('#answer').dispatchEvent(new Event('input')); document.querySelector('[data-action=submit]').click()");
  await wait("document.body.textContent.includes('No real conversation has been verified')");
  await assert("document.body.textContent.includes('@SyntheticDaemon_bot') && document.body.textContent.includes('/start')", 'Missing bot fallback');
  if (backend.smokeOpenedLinks.length !== 1) throw new Error('Instruction opened browser without user action');
  await evaluate("[...document.querySelectorAll('[data-link]')].find(e=>state.links[Number(e.dataset.link)]==='https://web.telegram.org/k/#@SyntheticDaemon_bot').click()");
  await wait("true");
  await new Promise(resolve => setTimeout(resolve, 100));
  if (backend.smokeOpenedLinks[1] !== 'https://web.telegram.org/k/#@SyntheticDaemon_bot') throw new Error('Wrong conversation destination');
  await capture('06-conversation');
  await evaluate("document.querySelector('[data-action=submit]').click()");
  await backend.stopped;
  await wait("document.body.textContent.includes('Transport test complete')");
  await assert("!document.body.textContent.includes('Setup complete.')", 'False completion');
  await assert("typeof require === 'undefined' && typeof process === 'undefined'", 'Renderer escaped sandbox');
  const folders = (await fs.readdir(path.join(output, 'memory'))).filter(s => !s.startsWith('.')).sort();
  if (folders.join(',') !== 'Archives,Areas,Garden,Projects,Resources') throw new Error('Wrong fresh vault');
  await capture('07-finished');
  await evaluate("changeTheme('light')");
  await wait("theme==='light' && !matchMedia('(prefers-color-scheme: dark)').matches");
  return { schema: 'daemon.desktop-connected.smoke.v1', status: 'passed', platform: process.platform,
    language, elapsed_ms: Math.round(performance.now() - started), screenshots: output,
    native_python_bootstrap: true, protocol: true, token_retry: true, fresh_vault: true,
    api_choices: true, api_key_retry: true, mistral_recommendation: true, native_credential_store: true,
    model_calls: 0, telegram_calls: 0, services_started: 0, real_installation_verified: false };
}
async function run(window, backend, instances) {
  const prepare = backend.prepare;
  const results = [];
  const traces = [];
  const originalAnswer = backend.answer;
  for (const language of ['en', 'fr']) {
    window.setSize(language === 'fr' ? 860 : 1040, language === 'fr' ? 680 : 780);
    const trace = []; traces.push(trace);
    backend.answer = function(answer) { trace.push({...answer, value:this.prompt?.kind === 'secret' ? '<secret>' : answer.value}); return originalAnswer.call(this, answer); };
    backend.prepare = prepare;
    backend.smokeOpenedLinks.length = 0;
    results.push(await scenario(window, backend, language));
    if (language === 'en') await window.webContents.reload();
  }
  if (JSON.stringify(traces[0]) !== JSON.stringify(traces[1])) throw new Error('Display language changed engine requests');
  backend.answer = originalAnswer;
  const output = results[0].screenshots;
  for (const name of await fs.readdir(results[1].screenshots)) {
    if (name.endsWith('.png')) await fs.copyFile(path.join(results[1].screenshots,name),path.join(output,'fr-'+name));
  }
  // Exercise cancellation and unknown-error presentation without a real service.
  const evaluate = async code => {try{return await window.webContents.executeJavaScript(code);}catch(error){throw new Error(code+": "+error.message);}};
  const wait = async expression => { for(let i=0;i<250;i++) { if(await evaluate(expression)) return; await new Promise(r=>setTimeout(r,60)); } throw new Error(expression); };
  await evaluate("action('instances')");await wait("!state.instanceBusy && !!document.querySelector('[data-setup-resume]')");await evaluate("document.querySelector('[data-setup-resume]').click()");
  await wait("!!document.querySelector('#answer')");
  await evaluate("window.confirm=()=>true; document.querySelector('[data-action=cancel]').click()");
  await backend.stopped;
  await wait("!state.active");
  if (!await evaluate("state.result.result==='cancelled' && !document.querySelector('#answer')")) throw new Error('Cancellation did not complete');
  backend.event({type:'finished',result:'error',message:'Unexpected synthetic code 19: C:\\test'});
  await wait("!!document.querySelector('[data-original]')");
  if (!await evaluate("document.body.textContent.includes('erreur technique inattendue')")) throw new Error('Missing translated error guidance');
  await fs.writeFile(path.join(output,'fr-08-error.png'),(await window.webContents.capturePage()).toPNG());
  backend.event({type:'finished',result:'installed',message:'Setup complete. You can continue talking with your daemon on Telegram.'});
  await wait("document.body.textContent.includes('Installation terminée.')");
  await fs.writeFile(path.join(output,'fr-09-completion-display.png'),(await window.webContents.capturePage()).toPNG());
  // Only synthetic instance profiles and fake scheduler are exposed to this UI trial.
  const {fixture}=require('./instances-fixture.cjs');
  const instanceFixture=await fixture(path.join(output,'instance-fixture'));
  Object.assign(instances,instanceFixture,{environment:{},records:[],rows:new Map()});
  // Shared identity has two hosts; an identical name in another vault stays separate.
  const separate=path.join(instanceFixture.home,'.hermes/profiles/second');
  await instanceFixture.write(path.join(separate,'config.yaml'),'model: synthetic\n');
  await fs.mkdir(path.join(separate,'plugins/daemon_core'),{recursive:true});
  const otherMemory=path.join(instanceFixture.home,'other-memory');
  await instanceFixture.write(path.join(otherMemory,'.daemon/core/daemon_identity.json'),JSON.stringify({name:'Aster'}));
  await instanceFixture.write(path.join(separate,'.env'),'DAEMON_CORE_MEMORY_ROOT="'+path.join(otherMemory,'.daemon')+'"\n');
  const healthRoot=path.join(instanceFixture.home,'technical-receipts');
  await fs.appendFile(path.join(instanceFixture.hermes,'.env'),'TELEGRAM_ALLOWED_USERS=12345\nDAEMON_CORE_CONTINUITY_RECEIPT_DIR="'+healthRoot+'"\n');
  const reference=value=>require('node:crypto').createHash('sha256').update(value).digest('hex').slice(0,16);
  const healthAt=new Date().toISOString();
  const healthIdentity={user_ref:reference('12345'),platform:'telegram',memory_root_ref:reference(await fs.realpath(path.join(instanceFixture.memory,'.daemon'))),source_root_ref:reference('')};
  await instanceFixture.write(path.join(healthRoot,'fixture.jsonl'),[
    {stage:'memory',status:'failed',details:{error_type:'JSONDecodeError',message:'never-export-this-token'},reason_code:'hook_failed'},
    {stage:'cycle',status:'failed',reason_code:'after_turn_failed'}
  ].map(e=>JSON.stringify({schema:'daemon.continuity-receipt.event.v1',turn_id:'synthetic-failure',at:healthAt,identity:healthIdentity,...e})).join('\n'));
  const instanceStart=performance.now();
  for(const lang of ['en','fr']) {
    await evaluate(`changeLanguage(${JSON.stringify(lang)})`);
    await wait(`document.documentElement.lang===${JSON.stringify(lang)}`);
    await evaluate("document.querySelector('[data-action=instances]').click()");
    await wait("!state.instanceBusy && document.querySelectorAll('[data-instance-card]').length===5");
    if(!await evaluate("document.querySelectorAll('[data-daemon-card]').length===2 && document.querySelectorAll('.hostless-card').length===2 && document.querySelector('[data-daemon-card] h2').textContent==='Aster' && document.querySelector('[data-daemon-card] .daemon-hosts').textContent.includes('OpenClaw')"))throw new Error('Daemon hierarchy or shared-host grouping failed');
    if(!await evaluate("document.body.scrollWidth<=window.innerWidth && document.querySelector('footer').getBoundingClientRect().bottom<=window.innerHeight"))throw new Error('Instances layout overflow');
    if(await evaluate("document.body.textContent.includes('never-export-this-token')"))throw new Error('Secret exposed by inventory');
    if(!await evaluate("document.querySelectorAll('.host-status strong').length===5 && [...document.querySelectorAll('.host-status strong')].every(s=>Number(getComputedStyle(s).fontWeight)>=600 && getComputedStyle(s).color===getComputedStyle(document.body).color)"))throw new Error('Host status lacks prominence');
    await new Promise(r=>setTimeout(r,220));
    await fs.writeFile(path.join(output,lang+'-10-instances.png'),(await window.webContents.capturePage()).toPNG());
  }
  for(const lang of ['en','fr']) {
    await evaluate(`changeLanguage(${JSON.stringify(lang)})`);
    await wait(`document.documentElement.lang===${JSON.stringify(lang)}`);
    await evaluate("document.querySelectorAll('.host-choices').forEach(d=>d.open=true);document.querySelector('[data-health]').click()");
    await wait("state.page==='health' && !!reportState.health && !reportState.busy");
    if(!await evaluate("reportState.health.checks.length===11 && reportState.health.checks.find(c=>c.key==='rem').status==='unknown'"))throw new Error('Missing health checks or false nightly failure');
    await evaluate("document.getAnimations().filter(a=>a.effect?.getTiming().iterations!==Infinity).forEach(a=>a.finish())");
    await evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');
    await layout(window);await contrast(window);
    await fs.writeFile(path.join(output,lang+'-health.png'),(await window.webContents.capturePage()).toPNG());
    if(!await evaluate("reportState.health.checks.find(c=>c.key==='after-turn').status==='issue'"))throw new Error('Synthetic failure not visible');
    if(!await evaluate("!!document.querySelector('.topbar button.crumb[data-daemon-manage]') && !!document.querySelector('.topbar button.crumb[data-action=instances]')"))throw new Error('Earlier steps of the location must lead back');
    // The page leads with one verdict and three plain answers; the checks stay folded below.
    if(!await evaluate("document.querySelectorAll('[data-health-area]').length===3 && document.querySelector('.health-verdict-problem') && document.querySelector('[data-health-area=memory]').classList.contains('health-area-problem') && document.querySelector('[data-health-area=memory] [data-health-export]') && !document.querySelector('.health-technical').open"))throw new Error('Health summary must lead with the memory problem and fold the checks');
    await evaluate("document.querySelector('[data-health-detail=technical]').open=true;document.querySelector('[data-health-detail=after-turn]').open=true;document.querySelector('[data-health-detail=after-turn]').scrollIntoView({block:'start'})");
    await wait("document.querySelector('[data-health-detail=after-turn]').open");
    if(!await evaluate("document.querySelector('[data-health-detail=after-turn]').textContent.includes('JSONDecodeError') && !document.querySelector('[data-health-start]')"))throw new Error('Missing exact safe cause or offered unsupported action');
    const focused=await evaluate("window.companion.exportDiagnostic('health',reportState.id,'after-turn')");
    if(!focused.exported||focused.report.checks.length!==1||focused.report.checks[0].key!=='after-turn'||JSON.stringify(focused).includes('never-export-this-token'))throw new Error('Targeted diagnostic export mismatch');
    await evaluate("document.querySelector('[data-health-export=after-turn]').click()");await wait("!reportState.busy && !!reportState.message");
    for(const mode of ['light','dark']) {
      await evaluate(`changeTheme(${JSON.stringify(mode)})`);await wait(`theme===${JSON.stringify(mode)} && !themeSaving`);
      await evaluate("document.querySelector('[data-health-detail=technical]').open=true;document.querySelector('[data-health-detail=after-turn]').open=true;document.querySelector('[data-health-detail=after-turn]').scrollIntoView({block:'start'})");
      await new Promise(resolve=>setTimeout(resolve,200));await layout(window);await contrast(window);
      await fs.writeFile(path.join(output,lang+'-'+mode+'-health-problem.png'),(await window.webContents.capturePage()).toPNG());
    }
    const report=await evaluate("window.companion.exportDiagnostic('health',reportState.id)");
    if(!report.exported||JSON.stringify(report).includes('never-export-this-token'))throw new Error('Health export failed');
    await evaluate("document.querySelector('[data-action=report-back]').click();document.querySelector('[data-setup-report]').click()");
    await wait("state.page==='diagnostic' && !!reportState.diagnostic && !reportState.busy");
    const diagnostic=await evaluate("window.companion.exportDiagnostic('setup',reportState.id)");
    if(!diagnostic.report.saved||!diagnostic.report.events.length||JSON.stringify(diagnostic).includes('synthetic-valid-api-key'))throw new Error('Installation diagnostic export failed');
    const reopened=new (require('./diagnostics.cjs').Diagnostics)(backend.diagnostics.root).read();
    if(!reopened.saved||JSON.stringify(reopened.events)!==JSON.stringify(diagnostic.report.events))throw new Error('Saved diagnostic did not survive reopening');
    await evaluate("document.getAnimations().filter(a=>a.effect?.getTiming().iterations!==Infinity).forEach(a=>a.finish())");
    await evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');
    await layout(window);await contrast(window);
    await fs.writeFile(path.join(output,lang+'-diagnostic.png'),(await window.webContents.capturePage()).toPNG());
    await evaluate("document.querySelector('[data-action=instances]').click()");await wait("!state.instanceBusy");
  }
  const rows=await instances.refresh();const hermes=rows.instances.find(r=>r.profile===instanceFixture.hermes);
  await instances.change(hermes.id,'connect',async()=>true);
  await evaluate("document.querySelector('[data-action=instance-refresh]').click()");
  await wait("!!document.querySelector('[data-instance-action=restart]') && !state.instanceBusy");
  await new Promise(r=>setTimeout(r,220));
  await fs.writeFile(path.join(output,'fr-11-instance-controls.png'),(await window.webContents.capturePage()).toPNG());
  const daemonSettings = await daemonSettingsTrial(window, backend, instances, instanceFixture, output);
  const network = await networkTrial(backend);
  const backup = await backupTrial(window, backend, instances, instanceFixture, output);
  const discard = await discardTrial(window, backend, instances, output);
  const removal = await removalTrial(window, backend, instances, output);
  if(instanceFixture.services.calls.length)throw new Error('UI inventory started a service');
  const instanceElapsed=Math.round(performance.now()-instanceStart);
  const savedSettingsRuntime=backend.settingsRuntime;backend.settingsRuntime=backend.smokeRuntime;
  const missingSource=path.join(output,'absent-source/profile');
  try {
    await assertRejectAttachment(backend,missingSource,output);
  } finally {backend.settingsRuntime=savedSettingsRuntime;}
  const hostInterfaceFeedback = await hostInterfaceFeedbackTrial(window,backend,output);
  const hostAddition = await hostAdditionTrial(window, backend, instances, output);
  const appearance = await appearanceTrial(window, backend, output);
  const newInstances = await newInstanceTrial(window, backend, instances, output);
  const testNode=(await require('node:util').promisify(require('node:child_process').execFile)('node',['-p','process.execPath'],{timeout:10000})).stdout.trim();
  const nativeInstances=await require('./instances-native.cjs').run({nodePath:testNode});
  return {schema:'daemon.desktop-localization.smoke.v1', status:'passed', platform:process.platform,
    screenshots:output, scenarios:results, host_interface_feedback:hostInterfaceFeedback, host_addition:hostAddition, appearance, new_instances:newInstances, instance_inventory:true, host_status_emphasized:true, health_and_diagnostic_exports:true,contextual_health_export:true,obsidian_ipc:true,external_obsidian_launch:false, daemon_settings:daemonSettings, network, backup, discard, removal, native_instances:nativeInstances, instance_ui_elapsed_ms:instanceElapsed, model_calls:0, telegram_calls:0, services_registered:process.platform==='darwin'?10:2, personal_services_started:0,
    engine_requests_identical:true, english_default:true, preference_persistence:true, draft_preservation:true, secret_preservation:true,
    cancellation:true, unknown_error_original_preserved:true, real_installation_verified:false};
}
async function layout(window) {
  const clipped = await window.webContents.executeJavaScript(`(() => {
    return [...document.querySelectorAll('.footer .button,.nav,.theme-toggle')].flatMap(el=>{
      const box=el.getBoundingClientRect(),range=document.createRange();range.selectNodeContents(el);const text=range.getBoundingClientRect();
      return box.left>=0 && box.right<=innerWidth+1 && box.bottom<=innerHeight+1 && text.left>=box.left-1 && text.right<=box.right+1 && text.bottom<=box.bottom+1 ? [] : [{label:el.textContent,box:{left:box.left,right:box.right,bottom:box.bottom},text:{left:text.left,right:text.right,bottom:text.bottom}}];
    });
  })()`);
  if(clipped.length)throw new Error('Clipped navigation: '+JSON.stringify(clipped));
  // Visible buttons that share a line keep a gap between them. A button counts
  // only where it is hit at its centre, so hidden or scrolled-away ones do not.
  const touching = await window.webContents.executeJavaScript(`(() => {
    const shown=el=>{const r=el.getBoundingClientRect();return r.width&&r.height&&el.contains(document.elementFromPoint(r.left+r.width/2,r.top+r.height/2));};
    const boxes=[...document.querySelectorAll('button')].filter(shown).map(el=>({label:el.textContent.trim(),box:el.getBoundingClientRect()}));
    return boxes.flatMap((a,i)=>boxes.slice(i+1).filter(b=>Math.min(a.box.bottom,b.box.bottom)-Math.max(a.box.top,b.box.top)>2 && Math.max(a.box.left,b.box.left)-Math.min(a.box.right,b.box.right)<8).map(b=>({labels:[a.label,b.label],boxes:[a.box,b.box].map(r=>[r.left,r.top,r.right,r.bottom].map(Math.round))})));
  })()`);
  if(touching.length)throw new Error('Touching buttons: '+JSON.stringify(touching));
}
// Inspect computed colors in the actual renderer, including transparent ancestors.
async function contrast(window) {
  const failures = await window.webContents.executeJavaScript(`(() => {
    const rgb = value => value.match(/[\\d.]+/g)?.map(Number) || [];
    const luminance = color => color.slice(0,3).map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4}).reduce((sum,v,i)=>sum+v*[.2126,.7152,.0722][i],0);
    const background = el => {for(let e=el;e;e=e.parentElement){const c=rgb(getComputedStyle(e).backgroundColor);if(c.length===3 || c[3]===1)return c;}return [255,255,255]};
    return [...document.querySelectorAll('.theme-toggle,.nav,.intro,.note,.hint,.small,.button,.text-button,.eyebrow,.about-section p,.about-meta dt,.status,.shared-badge,.field-label,.guidance,.footer-note,.daemon-feedback,.daemon-preferences summary,.preview-label,.welcome-next p,.welcome-existing p')].filter(el=>{
      const r=el.getBoundingClientRect();return !el.disabled && (el.textContent.trim() || el.matches('.theme-toggle')) && r.width && r.height && r.top>=0 && r.bottom<=innerHeight;
    }).flatMap(el=>{const a=luminance(rgb(getComputedStyle(el).color)),b=luminance(background(el));const ratio=(Math.max(a,b)+.05)/(Math.min(a,b)+.05);return ratio>=4.5?[]:[{element:el.className,text:el.textContent.slice(0,40),ratio}];});
  })()`);
  if(failures.length)throw new Error('Text contrast below 4.5: '+JSON.stringify(failures));
}
async function appearanceTrial(window, backend, output) {
  const started = performance.now();
  const {nativeTheme,Menu}=require('electron');
  const evaluate = async code => {try{return await window.webContents.executeJavaScript(code);}catch(error){throw new Error(code+": "+error.message);}};
  const wait = async expression=>{for(let i=0;i<250;i++){if(await evaluate(expression))return;await new Promise(r=>setTimeout(r,40));}throw new Error(expression+' '+JSON.stringify(await evaluate("({theme,dark:matchMedia('(prefers-color-scheme: dark)').matches,focused:document.activeElement?.tagName,controlFocused:document.activeElement?.matches('[data-theme-toggle]'),themeSaving})")));};
  const capture = async name=>{
    await evaluate("document.getAnimations().filter(a=>a.effect?.getTiming().iterations!==Infinity).forEach(a=>a.finish())");
    await new Promise(r=>setTimeout(r,150));
    if(!await evaluate("document.documentElement.scrollWidth<=innerWidth && document.querySelector('footer').getBoundingClientRect().bottom<=innerHeight+1"))throw new Error('Appearance layout overflow');
    await layout(window);
    await contrast(window);
    await fs.writeFile(path.join(output,name+'.png'),(await window.webContents.capturePage()).toPNG());
  };
  window.setSize(860,680);
  for(const lang of ['en','fr']) {
    await evaluate(`changeLanguage(${JSON.stringify(lang)})`);
    await wait(`document.documentElement.lang===${JSON.stringify(lang)}`);
    for(const mode of ['light','dark']) {
      await evaluate(`changeTheme(${JSON.stringify(mode)})`);
      await wait(`theme===${JSON.stringify(mode)} && matchMedia('(prefers-color-scheme: dark)').matches===${mode==='dark'}`);
      await window.webContents.reload();
      await wait(`!!document.querySelector('[data-action=settings]') && theme===${JSON.stringify(mode)} && document.documentElement.lang===${JSON.stringify(lang)} && matchMedia('(prefers-color-scheme: dark)').matches===${mode==='dark'}`);
      await capture(`${lang}-${mode}-welcome`);
      if (!await evaluate("!!document.querySelector('aside > .settings-button[data-action=settings]') && document.querySelectorAll('[data-language],[data-theme-choice]').length===0 && !document.querySelector('.titlebar,[data-theme-toggle]') && getComputedStyle(document.querySelector('.shell')).getPropertyValue('-webkit-app-region')==='drag' && getComputedStyle(document.querySelector('.settings-button')).getPropertyValue('-webkit-app-region')==='no-drag'")) throw new Error('Settings button, drag region or simplified frame is incorrect');
      await evaluate("document.querySelector('[data-action=about]').click()");
      if(!await evaluate("document.body.textContent.includes('Jonathan Pacifico') && document.body.textContent.includes(appInfo.revision) && document.querySelectorAll('.content [data-project-link]').length===3"))throw new Error('Missing About metadata or contact');
      await capture(`${lang}-${mode}-about`);
      await evaluate("document.querySelector('[data-action=settings]').click()");
      if(!await evaluate(`state.page==='settings' && document.querySelectorAll('[data-language]').length===1 && document.querySelectorAll('[data-theme-choice]').length===3 && document.querySelector('[data-theme-choice]:checked')?.value===${JSON.stringify(mode)} && document.body.textContent.includes(locale.t('update.disabled',language)) && document.body.textContent.includes(appInfo.revision)`))throw new Error('Settings must hold language, appearance and updates');
      await capture(`${lang}-${mode}-settings`);
      await evaluate("update={enabled:true,state:'current',channel:'stable'};render();document.querySelector('#settings-updates').scrollIntoView({block:'center'})");
      if(!await evaluate("document.querySelectorAll('#settings-updates .links [data-update-check],#settings-updates .links [data-update-export]').length===2"))throw new Error('Update actions must share one spaced row');
      await capture(`${lang}-${mode}-settings-updates`);
      if(!await evaluate("(()=>{const before=document.querySelector('.content').scrollTop;update={...update,state:'checking'};render();return before>0&&document.querySelector('.content').scrollTop===before;})()"))throw new Error('Settings must keep its scroll position when the update section changes');
      await evaluate("update={enabled:false,state:'disabled'};render()");
      await evaluate("document.querySelector('[data-action=instances]').click()");
      await wait("!state.instanceBusy && document.querySelectorAll('[data-instance-card]').length===5");
      await capture(`${lang}-${mode}-instances`);
      // The top bar says where you are; Getting started lists four steps read from the app's own state.
      if(!await evaluate("document.querySelector('.topbar .crumb-current')?.textContent===locale.t('topbar.daemons',language) && !!document.querySelector('[data-project-link=guide]')"))throw new Error('Top bar must show the location and help');
      await evaluate("document.querySelector('[data-started-toggle]').click()");
      await wait("started.open && Array.isArray(started.steps) && !started.loading && document.querySelectorAll('.started-panel li').length===4");
      await capture(`${lang}-${mode}-started`);
      if(!await evaluate("[...document.querySelectorAll('.started-panel li:not(.done)')].every(li=>li.querySelector('[data-started-step]'))"))throw new Error('Every unfinished step must lead to its action');
      await evaluate("started.open=false;render()");
      backend.event({type:'phase',name:'installation'});backend.event({type:'activity',active:true,message:'Preparing the next step…'});
      await evaluate("state.active=true;state.page='setup';render()");
      await capture(`${lang}-${mode}-progress`);
      backend.event({type:'idle'});
      backend.event({type:'finished',result:'error',message:'Unexpected synthetic error'});
      await wait("!!document.querySelector('details pre[data-original]') || document.body.textContent.includes('Unexpected synthetic error')");
      await capture(`${lang}-${mode}-error`);
    }
  }
  // Settings controls change the display in place.
  await evaluate("document.querySelector('[data-action=settings]').click()");
  await evaluate("document.querySelector('[data-language]').value='en';document.querySelector('[data-language]').dispatchEvent(new Event('change'))");
  await wait("document.documentElement.lang==='en' && state.page==='settings' && document.activeElement.matches('[data-language]')");
  await evaluate("document.querySelector('[data-theme-choice][value=light]').click()");
  await wait("theme==='light' && !matchMedia('(prefers-color-scheme: dark)').matches && document.querySelector('[data-theme-choice][value=light]').checked");
  await evaluate("document.querySelector('[data-theme-choice][value=dark]').click()");
  await wait("theme==='dark' && matchMedia('(prefers-color-scheme: dark)').matches && state.page==='settings'");
  // The appearance choice works from the keyboard: arrows move between modes.
  window.focus(); window.webContents.focus();
  await evaluate("document.querySelector('[data-theme-choice][value=dark]').focus()");
  window.webContents.sendInputEvent({type:'keyDown',keyCode:'Left'});
  window.webContents.sendInputEvent({type:'keyUp',keyCode:'Left'});
  await wait("theme==='light' && document.activeElement.matches('[data-theme-choice][value=light]')");
  // Existing System preferences still load; the new control offers two explicit modes.
  await evaluate("window.companion.theme('system')");
  await window.webContents.reload();
  await wait("!!document.querySelector('[data-action=settings]') && theme==='system'");
  if(nativeTheme.themeSource!=='system' || await evaluate("matchMedia('(prefers-color-scheme: dark)').matches")!==nativeTheme.shouldUseDarkColors)throw new Error('System appearance did not follow native theme');
  const systemDark = nativeTheme.shouldUseDarkColors;
  await evaluate("document.querySelector('[data-action=settings]').click()");
  if(!await evaluate("document.querySelector('[data-theme-choice]:checked').value==='system'"))throw new Error('System preference must show as selected');
  await evaluate(`document.querySelector('[data-theme-choice][value=${systemDark ? 'light' : 'dark'}]').click()`);
  await wait(`theme===${JSON.stringify(systemDark ? 'light' : 'dark')} && matchMedia('(prefers-color-scheme: dark)').matches===${!systemDark}`);
  const menu=Menu.getApplicationMenu();
  const about=process.platform==='darwin'?menu.items[0].submenu.items[0]:menu.items.at(-1).submenu.items[0];
  about.click();await wait("state.page==='about'");
  const before=backend.smokeOpenedLinks.length;
  await evaluate("document.querySelector('[data-project-link=contact]').click();document.querySelector('[data-project-link=project]').click()");
  for(let i=0;i<50&&backend.smokeOpenedLinks.length<before+2;i++)await new Promise(r=>setTimeout(r,20));
  if(backend.smokeOpenedLinks.slice(before).join(',')!=='https://github.com/jpacifico/daemon-harness-canonical/issues,https://github.com/jpacifico/daemon-harness-canonical')throw new Error('Wrong About destinations');
  return {status:'passed',elapsed_ms:Math.round(performance.now()-started),themes:['light','dark'],legacy_system_preserved:true,theme_keyboard:true,settings_page:true,frame_without_title_band:true,languages:['en','fr'],minimum_window:true,contrast_minimum:4.5,theme_reload:true,system_resolved_dark:systemDark,native_about_menu:true,fixed_links:true};
}
async function newInstanceTrial(window, backend, instances, output) {
  const started=performance.now(),assert=require('node:assert/strict');
  const {SetupHomes}=require('./setup-homes.cjs');
  const {installedSetup}=require('./setup-homes-fixture.cjs');
  const evaluate=code=>window.webContents.executeJavaScript(code);
  const wait=async expression=>{for(let i=0;i<250;i++){if(await evaluate(expression))return;await new Promise(r=>setTimeout(r,40));}throw new Error('Daemon inventory: '+expression);};
  const capture=async name=>{await evaluate("document.getAnimations().filter(a=>a.effect?.getTiming().iterations!==Infinity).forEach(a=>a.finish())");await new Promise(r=>setTimeout(r,220));await layout(window);await contrast(window);await fs.writeFile(path.join(output,name+'.png'),(await window.webContents.capturePage()).toPNG());};
  const runtime=backend.smokeRuntime,fixture=path.join(output,'host-offer-fixture.py');
  await fs.writeFile(fixture,await fs.readFile(path.join(__dirname,'host-offer-fixture.py')));
  window.setSize(860,680);
  for(const lang of ['en','fr']) {
    const base=path.join(output,'creation-'+lang,'DaemonHarness');backend.setupHomes=new SetupHomes(path.join(base,'Companion'));
    Object.assign(instances,{home:path.join(output,'creation-'+lang,'home'),base,environment:{},records:[],rows:new Map(),services:{async list(){return {available:true,items:[]};}}});
    let kind='absent';
    backend.prepare=async()=>({python:runtime.python,env:{...runtime.env,DAEMON_HARNESS_COMPANION_INSTALL_HOME:backend.installHome},
      args:['-I','-B','-u','-c','import sys,runpy;sys.path.insert(0,sys.argv.pop(1));sys.path.insert(1,sys.argv.pop(1));p=sys.argv.pop(1);runpy.run_path(p,run_name="__main__")',path.join(backend.packageRoot,'payload/src'),runtime.libraries,fixture,kind]});
    await evaluate(`window.companion.language(${JSON.stringify(lang)})`);await window.webContents.reload();
    await wait(`!!state.inventory && !state.instanceBusy && language===${JSON.stringify(lang)}`);
    assert(await evaluate("state.page==='instances' && document.querySelectorAll('nav .nav').length===2 && !document.querySelector('[data-action=welcome]') && !document.querySelector('[data-action=diagnostic]') && !document.querySelector('[data-setup-resume]') && !!document.querySelector('.footer [data-action=new-instance]')"));
    await capture(lang+'-my-daemons-empty');
    await evaluate("document.querySelector('[data-action=new-instance]').click()");await wait("state.prompt?.title==='Install Hermes 0.21.5?'");
    assert(await evaluate("!document.querySelector('.activity-details') && document.body.textContent.includes(locale.t('setup.duration',language))"));
    await capture(lang+'-host-duration-guidance');
    const first=backend.installHome;
    await evaluate("action('instances')");await wait("!state.instanceBusy");
    assert(await evaluate("!document.querySelector('[data-action=new-instance]') && !!document.querySelector('.footer [data-action=setup]')"));
    await evaluate("document.querySelector('.footer [data-action=setup]').click()");assert.equal(backend.installHome,first);
    await evaluate("document.querySelector('[name=answer][value=\"0\"]').click();document.querySelector('[data-action=submit]').click()");await wait("!state.active");await backend.stopped;
    assert.deepEqual((await fs.readdir(first)).filter(x=>x!=='diagnostics'),[]);
    await evaluate("action('instances')");await wait("!state.instanceBusy");
    await evaluate("document.querySelector('[data-action=new-instance]').click()");await wait("!!state.prompt");
    const second=backend.installHome;assert.notEqual(first,second);
    await evaluate("document.querySelector('[data-action=submit]').click()");
    await wait("!document.querySelector('[role=progressbar]') && !!document.querySelector('#activity .spinner') && activityOutputAt!==null");
    await wait("document.querySelector('[data-activity-elapsed]')?.textContent==='0:01'");
    assert(await evaluate("!document.querySelector('[role=progressbar]') && !!document.querySelector('[data-activity-elapsed]') && !state.prompt"));
    await capture(lang+'-host-installation-progress');
    await wait("!state.active");await backend.stopped;
    const receipt=await fs.readFile(path.join(second,'synthetic-host-confirmed.json'),'utf8');
    const rows=await backend.setupHomes.list(),firstId=rows.find(r=>backend.setupHomes.roots.get(r.id)===first).id,secondId=rows.find(r=>backend.setupHomes.roots.get(r.id)===second).id;
    await window.webContents.reload();await wait("!state.instanceBusy && state.inventory?.setups.length===2");
    assert(await evaluate("document.querySelectorAll('[data-setup-resume]').length===2 && document.querySelectorAll('[data-setup-report]').length===2"));
    await capture(lang+'-two-unfinished-daemons');
    await evaluate(`document.querySelector('[data-setup-report="${firstId}"]').click()`);await wait("!!reportState.diagnostic&&!reportState.busy");
    const report=await evaluate("window.companion.exportDiagnostic('setup',reportState.id)");assert.equal(report.report.status,'unfinished');assert.deepEqual(report.report,new (require('./diagnostics.cjs').Diagnostics)(path.join(first,'diagnostics')).read());assert.equal(await backend.setupHomes.current(),second);
    await evaluate("document.querySelector('[data-action=report-back]').click()");
    await evaluate(`document.querySelector('[data-setup-resume="${firstId}"]').click()`);await wait("!!state.prompt");assert.equal(backend.installHome,first);
    await evaluate("document.querySelector('[name=answer][value=\"0\"]').click();document.querySelector('[data-action=submit]').click()");await wait("!state.active");await backend.stopped;
    assert.equal(await fs.readFile(path.join(second,'synthetic-host-confirmed.json'),'utf8'),receipt);
    const proof=await installedSetup(second);const originals=await Promise.all(Object.values(proof.files).map(f=>fs.readFile(f,'utf8')));
    // Read-only startup cannot restart a completed setup, even when another is unfinished.
    await window.webContents.reload();await wait("!state.instanceBusy && !!state.inventory");
    assert(await evaluate(`!document.querySelector('[data-setup-resume="${secondId}"]') && !!document.querySelector('[data-setup-resume="${firstId}"]')`));
    assert.deepEqual(await Promise.all(Object.values(proof.files).map(f=>fs.readFile(f,'utf8'))),originals);
    await fs.writeFile(proof.files.receipt,'{');await window.webContents.reload();await wait("!state.instanceBusy && !!state.inventory");
    assert(await evaluate(`!document.querySelector('[data-setup-resume="${secondId}"]') && !!document.querySelector('[data-action=new-instance]')`));
    await capture(lang+'-unreadable-daemon-setup');
    kind='first-choice';
    await evaluate("action('instances')");await wait("!state.instanceBusy");
    await evaluate("document.querySelector('[data-action=new-instance]').click()");
    await wait("state.prompt?.title===\"1. Choose your daemon's host\"");
    assert(await evaluate("document.querySelectorAll('[name=answer]').length===2 && document.body.textContent.includes('OpenClaw') && document.body.textContent.includes('Hermes Agent')"));
    assert.equal(await evaluate("document.querySelector('h1')?.textContent"),lang==='fr'?'Installez l’hôte de votre daemon.':'Install your daemon’s host.');
    await capture(lang+'-first-host-choice');
    await evaluate("document.querySelector('[name=answer][value=\"1\"]').click();document.querySelector('[data-action=submit]').click()");
    await wait("state.prompt?.title==='Install OpenClaw?'");
    await capture(lang+'-first-openclaw-confirmation');
    await evaluate("document.querySelector('[name=answer][value=\"1\"]').click();document.querySelector('[data-action=submit]').click()");
    await wait("state.prompt?.title===\"1. Choose your daemon's host\"");
    await evaluate("window.companion.cancel()");await wait("!state.active");await backend.stopped;
  }
  return {status:'passed',elapsed_ms:Math.round(performance.now()-started),empty_inventory:true,separate_roots:true,resume_older_root_after_reload:true,diagnostic_bound_to_root:true,report_read_does_not_select:true,completed_not_resumable:true,unreadable_not_resumable:true,existing_profiles_preserved:true,real_host_download:false,model_calls:0,telegram_calls:0};
}
async function runDiscard(window,backend,instances) {
  const output=await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(),'daemons-discard-native-')));
  backend.smokeRuntime=await backend.prepare(new AbortController().signal);
  window.setSize(1040,780);
  return {schema:'daemon.desktop-discard.smoke.v1',screenshots:output,...await discardTrial(window,backend,instances,output)};
}
async function runRemoval(window,backend,instances) {
  const output=await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(),'daemons-removal-native-')));
  backend.smokeRuntime=await backend.prepare(new AbortController().signal);
  window.setSize(1040,780);
  return {schema:'daemon.desktop-removal.smoke.v1',screenshots:output,...await removalTrial(window,backend,instances,output)};
}
module.exports = {run,runDiscard,runRemoval};

async function daemonSettingsTrial(window,backend,instances,fixture,output) {
  const started=performance.now();
  const assert=require('node:assert/strict');
  const {dialog,nativeTheme}=require('electron');
  const evaluate=code=>window.webContents.executeJavaScript(code);
  const wait=async expression=>{for(let i=0;i<300;i++){if(await evaluate(expression))return;await new Promise(r=>setTimeout(r,50));}throw new Error('Daemon settings: '+expression+' '+await evaluate('state.error'));};
  const source=path.join(backend.packageRoot,'payload','src');
  await fixture.write(path.join(fixture.hermes,'.env'),'DAEMON_CORE_MEMORY_ROOT="'+path.join(fixture.memory,'.daemon')+'"\nDAEMON_CORE_SRC="'+source+'"\nTELEGRAM_ALLOWED_USERS=12345\nSECRET=never-export-this-token\n');
  fixture.services.items.find(s=>s.label.includes('openclaw-api')).env.DAEMON_CORE_SRC=source;
  const clawConfig=JSON.parse(await fs.readFile(path.join(fixture.openclaw,'openclaw.json'),'utf8'));clawConfig.plugins.entries['daemon-harness'].config.userId='claw-owner';await fixture.write(path.join(fixture.openclaw,'openclaw.json'),JSON.stringify(clawConfig));
  const identity=path.join(fixture.memory,'.daemon/core/daemon_identity.json');
  const original=await fs.readFile(identity);
  const exact=new Map();
  async function collect(root){for(const e of await fs.readdir(root,{withFileTypes:true})){const f=path.join(root,e.name);if(e.isDirectory())await collect(f);else if(e.isFile())exact.set(f,await fs.readFile(f));}}
  await collect(fixture.home);
  backend.settingsRuntime=backend.smokeRuntime;
  let approved=false,confirmations=0;
  const originalDialog=dialog.showMessageBox;
  const settingsMethod=backend.daemonSettings;
  let releaseSave;
  dialog.showMessageBox=async(_parent,options)=>{confirmations++;assert.equal(options.defaultId,0);assert(options.detail.includes(fixture.hermes)&&options.detail.includes(fixture.openclaw));return {response:approved?1:0};};
  const capture=async name=>{
    await evaluate("document.getAnimations().filter(a=>a.effect?.getTiming().iterations!==Infinity).forEach(a=>a.finish())");
    await new Promise(r=>setTimeout(r,150));await layout(window);await contrast(window);
    assert(await evaluate('document.documentElement.scrollWidth<=innerWidth'));
    await fs.writeFile(path.join(output,name+'.png'),(await window.webContents.capturePage()).toPNG());
  };
  try {
    const inventory=await instances.refresh();const hermes=inventory.instances.find(r=>r.profile===fixture.hermes);
    const claw=inventory.instances.find(r=>r.profile===fixture.openclaw);
    await evaluate('state.active=false;state.error="";state.page="instances";render()');
    assert(await evaluate("document.querySelector('h1').textContent===locale.message('My daemons',language)"));
    await evaluate(`openDaemon(${JSON.stringify(hermes.id)})`);
    await wait('!!state.daemon && !state.daemonBusy');
    assert(await evaluate('state.daemon.canEditName && state.daemon.canEditUser && state.daemon.shared.length===1'));
    assert(await evaluate("!!document.querySelector('[data-instance-open=memory]') && document.querySelector('[data-action=daemon-save]').disabled"));
    assert(await evaluate("!document.querySelector('#daemon-pronouns') && state.daemon.values.pronouns==='they/them' && state.daemon.accounts.length===2 && state.daemon.accounts.filter(a=>a.addressing).length===1"));
    assert.equal(await evaluate('window.companion.copyMemoryPath(state.daemon.id)'),fixture.memory);
    await evaluate("document.querySelector('[data-instance-open=memory]').click()");
    assert(await evaluate('obsidianInfo.available && !obsidianInfo.registered'));
    const obsidianConfig=path.join(require('electron').app.getPath('userData'),'obsidian','obsidian.json');
    await fs.mkdir(path.dirname(obsidianConfig),{recursive:true});
    const obsidianRaw=JSON.stringify({language:'en',vaults:{aaaaaaaaaaaaaaaa:{path:fixture.openclaw,open:false}}});
    await fs.writeFile(obsidianConfig,obsidianRaw);
    backend.smokeObsidianRunning=true;
    for(const displayLanguage of ['en','fr']) {
      await evaluate(`changeLanguage(${JSON.stringify(displayLanguage)})`);
      await wait(`document.documentElement.lang===${JSON.stringify(displayLanguage)}`);
      await evaluate("document.querySelector('[data-obsidian-open]').click()");
      await wait("obsidianMessage==='obsidian.closeFirst' && !obsidianBusy");
      assert(await evaluate("document.querySelector('.obsidian-help').textContent.split(locale.t('obsidian.first',language)).length===2"));
      assert.equal(await fs.readFile(obsidianConfig,'utf8'),obsidianRaw);
    }
    backend.smokeObsidianRunning=false;
    await evaluate("document.querySelector('[data-obsidian-open]').click()");
    await wait("obsidianMessage==='obsidian.added' && !obsidianBusy && obsidianInfo.registered");
    assert(await evaluate("!document.querySelector('.obsidian-help').textContent.includes(locale.t('obsidian.first',language))"));
    const registered=await fs.readFile(obsidianConfig,'utf8');
    const backups=(await fs.readdir(path.dirname(obsidianConfig))).filter(f=>f.startsWith('obsidian.daemons-backup-'));
    assert.equal(backups.length,1);assert.equal(await fs.readFile(path.join(path.dirname(obsidianConfig),backups[0]),'utf8'),obsidianRaw);
    const obsidianResult=await evaluate('window.companion.openObsidian(state.daemon.id)');assert(obsidianResult.registered&&!obsidianResult.added);
    assert.equal(await fs.readFile(obsidianConfig,'utf8'),registered);

    // Model section, with a synthetic provider runner: no credential store, network or service.
    const providerCalls=[];let restartFails=false;
    backend.smokeProvider={
      run:async request=>{
        providerCalls.push({...request,key:request.key?'set':request.key??'absent'});
        const committed=providerCalls.some(c=>c.action==='commit');
        if(request.action==='describe')return {kind:'mistral_api',model:committed?'mistral-medium-3':'mistral-large-4',editable:true,key:true,pending:false,revision:'r'+providerCalls.length};
        if(request.action==='check')return {models:['mistral-large-4','mistral-medium-3'],available:true};
        if(request.action==='change')return {status:'applied'};
        return {status:request.action==='commit'?'committed':'reverted'};
      },
      restart:async()=>{if(restartFails)throw new Error('provider.restart-failed');return true;}};
    const changeModel=async(shot=null)=>{
      await evaluate("document.querySelector('[data-provider-edit]').click()");
      await evaluate("{const k=document.querySelector('#provider-key');k.value='synthetic-new-key';k.dispatchEvent(new Event('input'))}");
      await evaluate("document.querySelector('[data-provider-models]').click()");
      await wait("providerState.models?.length===2 && !providerState.busy");
      await evaluate("{const m=document.querySelector('#provider-model');m.value='mistral-medium-3';m.dispatchEvent(new Event('change'))}");
      if(shot){await evaluate("document.querySelector('.daemon-model').scrollIntoView({block:'start'})");await capture(shot);}
      await evaluate("document.querySelector('#provider-form').requestSubmit()");
    };
    await evaluate('loadProvider(state.daemon.id).then(render)');
    await wait("!!providerInfo && !!document.querySelector('[data-provider-edit]')");
    assert(await evaluate("['Mistral AI','mistral-large-4'].every(t=>document.querySelector('.daemon-model').textContent.includes(t))"));
    await evaluate("document.querySelector('[data-provider-check]').click()");
    await wait("providerState.checked?.available===true && !providerState.busy");
    restartFails=true;await changeModel();
    await wait("String(providerState.error).includes('provider.restart-failed') && !providerState.busy");
    assert.deepEqual(providerCalls.filter(c=>!['describe','check'].includes(c.action)).map(c=>c.action),['change','revert'],'A failed restart must revert the change');
    assert(await evaluate("document.querySelector('.daemon-model [role=alert]').textContent===locale.t('provider.restart-failed',language)"));
    restartFails=false;providerCalls.length=0;
    await evaluate('providerState={};render()');
    await changeModel('daemon-model-form');
    await wait("providerState.done?.key==='provider.saved' && !providerState.busy");
    assert.deepEqual(providerCalls.filter(c=>!['describe','check'].includes(c.action)).map(c=>c.action),['change','commit']);
    assert.equal(providerCalls.find(c=>c.action==='change').key,'set');
    assert(await evaluate("providerInfo.model==='mistral-medium-3' && !providerState.key && !document.querySelector('#provider-key')"));
    await evaluate("document.querySelector('.daemon-model').scrollIntoView({block:'start'})");await capture('daemon-model-saved');
    // Another provider: a checked request opens the guided session for this daemon.
    {
      const realRun=backend.smokeProvider.run,realStart=backend.start,starts=[];
      backend.smokeProvider.run=async request=>request.action==='describe'?{...await realRun(request),switchable:true,offers:['codex_cli','openai_api','anthropic_api']}:realRun(request);
      backend.start=async(...args)=>{starts.push(args);return {started:true};};
      try {
        await evaluate('loadProvider(state.daemon.id).then(render)');
        await wait("providerInfo?.switchable===true && !!document.querySelector('[data-provider-switch]')");
        assert(await evaluate("document.querySelector('[data-provider-switch]').textContent===locale.t('provider.switch',language) && document.querySelector('.daemon-model').textContent.includes(locale.t('provider.switchHint',language))"));
        await evaluate("document.querySelector('.daemon-model').scrollIntoView({block:'start'})");await capture('daemon-model-switch');
        await evaluate("document.querySelector('[data-provider-switch]').click()");
        await wait("state.page==='setup' && state.mode==='change-provider'");
        for(let i=0;i<200&&!starts.length;i++)await new Promise(r=>setTimeout(r,50));
        assert.equal(starts.length,1);
        assert.deepEqual(starts[0].slice(0,6),['change-provider',null,null,null,false,null]);
        assert.equal(starts[0][6].profile,fixture.hermes);assert.equal(starts[0][6].base,instances.base);
        assert(await evaluate("document.querySelector('h1').textContent===locale.message('Choose another model provider.',language)"));
        await capture('provider-switch-start');
      } finally {backend.smokeProvider.run=realRun;backend.start=realStart;}
      await evaluate('state.active=false;state.setupStarted=false;state.mode=\'setup\'');
      await evaluate(`openDaemon(${JSON.stringify(hermes.id)})`);await wait('!!state.daemon && !state.daemonBusy');
    }
    backend.smokeProvider=null;
    await evaluate('loadProvider(state.daemon.id).then(render)');
    await wait("providerInfo===null && document.querySelector('.daemon-model').textContent.includes(locale.t('provider.unavailableInfo',language))");

    // Host change: refused for a daemon on two hosts; otherwise a checked request opens the guided setup.
    assert(await evaluate("document.querySelector('.daemon-host').textContent.includes(locale.t('change.shared',language))"));
    const changeCalls=[],realStart=backend.start,realContext=instances.removalContext;
    instances.removalContext=async id=>({row:{...instances.rows.get(id)}});
    backend.daemonSettings=async function(request,mode){if(mode==='change'){changeCalls.push(request);return {name:'Nova',source:'hermes',target:'openclaw',kind:'mistral_api'};}return settingsMethod.call(this,request,mode);};
    backend.start=async(...args)=>{changeCalls.push(args);return {started:true};};
    try {
      await evaluate('state.daemon.shared=[];render()');
      assert(await evaluate("document.querySelector('[data-action=daemon-host-change]').textContent===locale.t('change.start',language,{to:'OpenClaw'})"));
      await evaluate("document.querySelector('.daemon-host').scrollIntoView({block:'start'})");await capture('daemon-host-change');
      await evaluate("document.querySelector('[data-action=daemon-host-change]').click()");
      await wait("state.page==='setup'");
      assert.deepEqual(changeCalls[0],{action:'inspect',profile:fixture.hermes,base:instances.base});
      assert.deepEqual(changeCalls[1],['new',null,null,null,false,{profile:fixture.hermes,base:instances.base}]);
    } finally {backend.start=realStart;instances.removalContext=realContext;backend.daemonSettings=settingsMethod;}
    await evaluate('state.active=false;state.setupStarted=false');
    await evaluate(`openDaemon(${JSON.stringify(hermes.id)})`);await wait('!!state.daemon && !state.daemonBusy');

    for(const language of ['en','fr'])for(const mode of ['light','dark']) {
      await evaluate(`changeLanguage(${JSON.stringify(language)})`);
      await wait(`document.documentElement.lang===${JSON.stringify(language)}`);
      await evaluate(`changeTheme(${JSON.stringify(mode)})`);
      await wait(`theme===${JSON.stringify(mode)} && !themeSaving`);
      await capture(`${language}-${mode}-daemon-identity`);
      await evaluate("document.querySelector('.daemon-accounts').open=true;document.querySelector('#daemon-preferred_name').scrollIntoView({block:'start'})");
      await capture(`${language}-${mode}-daemon-addressing`);
      await evaluate("document.querySelector('.daemon-memory').scrollIntoView({block:'start'})");
      await capture(`${language}-${mode}-daemon-memory`);
    }
    await evaluate("document.querySelector('#daemon-name').value='Étoile';document.querySelector('#daemon-name').dispatchEvent(new Event('input'));document.querySelector('#daemon-preferred_name').value='Robin';document.querySelector('#daemon-preferred_name').dispatchEvent(new Event('input'))");
    await evaluate("changeLanguage('en')");await wait("document.documentElement.lang==='en'");
    assert(await evaluate("document.querySelector('#daemon-name').value==='Étoile' && document.querySelector('#daemon-preferred_name').value==='Robin'"));
    await evaluate("changeTheme('light')");await wait("theme==='light' && !themeSaving");
    assert(await evaluate("state.daemonDraft.pronouns==='they/them'"));
    // Cancel the actual main-process confirmation; no mutation should occur.
    await evaluate('saveDaemon()');await wait('!state.daemonBusy');
    assert.deepEqual(await fs.readFile(identity),original);assert(await evaluate('daemonDirty()'));
    approved=true;
    let pendingSaves=0;
    // Hold only the synthetic writer boundary to observe the real UI and IPC.
    backend.daemonSettings=async function(request) {
      if(request.action==='save') {pendingSaves++;await new Promise(resolve=>{releaseSave=resolve;});}
      return settingsMethod.call(this,request);
    };
    await evaluate("document.querySelector('.content').scrollTop=100000;document.querySelector('[data-action=daemon-save]').click();void 0");
    await wait('state.daemonBusy');
    for(let i=0;i<100&&!releaseSave;i++)await new Promise(r=>setTimeout(r,20));
    assert(releaseSave,'Save did not reach writer');
    assert(await evaluate("document.querySelector('#daemon-status').textContent==='Saving…' && !!document.querySelector('#daemon-status .spinner') && document.querySelector('#daemon-preferred_name').disabled && document.querySelector('.content').scrollTop>0"));
    assert(await evaluate("(()=>{const r=document.querySelector('#daemon-status').getBoundingClientRect();return r.top>0&&r.bottom<innerHeight;})()"));
    await evaluate("document.querySelector('[data-action=daemon-save]').click();void 0");assert.equal(pendingSaves,1);
    await capture('en-light-daemon-saving-footer');
    const saveStart=performance.now();releaseSave();releaseSave=null;
    await wait('!state.daemonBusy');backend.daemonSettings=settingsMethod;
    assert(await evaluate("!state.error && !daemonDirty() && state.daemon.values.name==='Étoile' && !!state.daemonMessage"),'Saved settings mismatch: '+await evaluate('JSON.stringify({error:state.error,values:state.daemon?.values,draft:state.daemonDraft,message:state.daemonMessage})'));
    const saveMs=Math.round(performance.now()-saveStart);
    let saved=JSON.parse(await fs.readFile(identity,'utf8'));
    assert.equal(saved.name,'Étoile');assert.equal(saved.user.preferred_name,'Robin');assert.equal(saved.user.user_id,'12345');
    assert.equal(confirmations,2);
    assert(await evaluate("document.querySelector('#daemon-status').textContent==='Changes saved. You can keep editing.' && document.querySelector('[data-action=daemon-save]').textContent==='Saved' && !document.querySelector('#daemon-preferred_name').disabled && document.querySelector('.content').scrollTop>0"));
    await capture('en-light-daemon-saved-footer');
    // A second edit must enable Save without reopening, including failure/retry.
    await evaluate("document.querySelector('#daemon-preferred_name').value='Robin II';document.querySelector('#daemon-preferred_name').dispatchEvent(new Event('input'))");
    assert(await evaluate("!document.querySelector('[data-action=daemon-save]').disabled && document.querySelector('#daemon-status').textContent==='Unsaved changes.'"));
    backend.daemonSettings=async function(request) {if(request.action==='save')throw new Error('Synthetic save failure');return settingsMethod.call(this,request);};
    await evaluate("document.querySelector('[data-action=daemon-save]').click();void 0");await wait("!state.daemonBusy && !!state.error");
    backend.daemonSettings=settingsMethod;
    assert(await evaluate("document.querySelector('#daemon-error').textContent.includes('Synthetic save failure') && !document.querySelector('#daemon-preferred_name').disabled && !document.querySelector('[data-action=daemon-save]').disabled && document.querySelector('#daemon-preferred_name').value==='Robin II'"));
    assert.equal(JSON.parse(await fs.readFile(identity,'utf8')).user.preferred_name,'Robin');
    await capture('en-light-daemon-save-error');
    const retryStart=performance.now();
    await evaluate("document.querySelector('[data-action=daemon-save]').click();void 0");await wait('!state.daemonBusy');
    assert(await evaluate('!state.error && !daemonDirty()'));
    const retryMs=Math.round(performance.now()-retryStart);
    saved=JSON.parse(await fs.readFile(identity,'utf8'));assert.equal(saved.user.preferred_name,'Robin II');
    // Reopen through the other host; the same metadata and original account remain.
    await evaluate(`openDaemon(${JSON.stringify(claw.id)})`);await wait('!!state.daemon && !state.daemonBusy');
    assert(await evaluate("state.daemon.values.name==='Étoile' && state.daemon.values.preferred_name==='Robin II' && state.daemon.userScope.platform==='telegram'"));
    await capture('fr-daemon-saved-other-host');
    // Removed controls must not erase previously saved preferences on name edits.
    assert.equal(saved.user.pronouns,'they/them');
    await evaluate("changeLanguage('fr');changeTheme('dark')");
    await wait("document.documentElement.lang==='fr' && theme==='dark' && !themeSaving");
    assert(await evaluate("!document.querySelector('#daemon-pronouns') && !document.body.textContent.includes('Vos pronoms')"));
    await capture('fr-dark-daemon-addressing');
    const concurrent={...saved,name:'External edit'};await fs.writeFile(identity,JSON.stringify(concurrent));
    await evaluate("document.querySelector('#daemon-name').value='Stale form';document.querySelector('#daemon-name').dispatchEvent(new Event('input'));saveDaemon()");await wait('!state.daemonBusy');
    assert(await evaluate("state.error.includes('changed') && daemonDirty()"));assert.equal(JSON.parse(await fs.readFile(identity,'utf8')).name,'External edit');
    await capture('daemon-concurrent-edit-refused');
    for(const [file,bytes] of exact)if(file!==identity)assert.deepEqual(await fs.readFile(file),bytes);
    assert.equal(fixture.services.calls.length,0);
    // Keep later inventory/appearance trials independent of these test edits.
    await fs.writeFile(identity,original);await evaluate("state.daemon=null;state.daemonDraft=null;state.error='';state.page='instances';render()");
    await evaluate("changeTheme('light')");await wait("theme==='light' && !themeSaving");
    return {status:'passed',elapsed_ms:Math.round(performance.now()-started),save_ms:saveMs,real_packaged_ipc:true,
      real_python_writer:true,fixed_footer_feedback:true,save_spinner:true,duplicate_save_blocked:true,second_edit_and_save:true,failure_retry:true,retry_ms:retryMs,scroll_preserved:true,pronoun_controls_removed:true,shared_confirmation:true,cancel_preserved:true,reopen_other_host:true,
      existing_preferences_preserved:true,account_list_without_linking:true,
      concurrent_edit_preserved:true,other_files_unchanged:true,host_change_started:true,model_calls:0,personal_services_changed:0};
  }finally{releaseSave?.();backend.daemonSettings=settingsMethod;dialog.showMessageBox=originalDialog;nativeTheme.themeSource='light';}
}

async function removalTrial(window,backend,instances,output) {
  if(process.platform!=='darwin')return {status:'not-qualified',reason:'Native Windows qualification required'};
  const assert=require('node:assert/strict');
  const {promisify}=require('node:util'),{execFile}=require('node:child_process');const exec=promisify(execFile);
  const {NativeServices}=require('./instances.cjs');
  const started=performance.now(),root=path.join(output,'removal-trial');
  await fs.mkdir(root);
  const fixtureFile=path.join(root,'fixture.py');await fs.writeFile(fixtureFile,await fs.readFile(path.join(__dirname,'removal-fixture.py')));
  const runtime=backend.smokeRuntime;
  const invoke=async args=>JSON.parse((await exec(runtime.python,['-I','-B','-c','import sys,runpy;sys.path.insert(0,sys.argv.pop(1));sys.path.insert(1,sys.argv.pop(1));p=sys.argv.pop(1);runpy.run_path(p,run_name="__main__")',path.join(backend.packageRoot,'payload/src'),runtime.libraries,fixtureFile,...args],{env:runtime.env,timeout:60000})).stdout);
  const fixture=await invoke(['create',path.join(root,'owned')]);
  const metadata=path.join(root,'metadata.json');await fs.writeFile(metadata,JSON.stringify(fixture));
  const prior={home:instances.home,base:instances.base,services:instances.services,records:instances.records,environment:instances.environment};
  const priorRuntime=backend.settingsRuntime,priorHomes=backend.setupHomes;
  const evaluate=code=>window.webContents.executeJavaScript(code);
  const wait=async expression=>{for(let i=0;i<400;i++){if(await evaluate(expression))return;await new Promise(r=>setTimeout(r,50));}throw new Error('Removal UI: '+expression+' '+await evaluate('state.error'));};
  const captureRemoval=async name=>{
    await evaluate("document.querySelector('.content').getBoundingClientRect();document.getAnimations().filter(a=>a.effect?.getTiming().iterations!==Infinity).forEach(a=>a.finish())");
    await evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
    await new Promise(resolve=>setTimeout(resolve,200));
    await layout(window);await contrast(window);
    assert(await evaluate("Number(getComputedStyle(document.querySelector('.content')).opacity)===1"));
    await fs.writeFile(path.join(output,name),(await window.webContents.capturePage()).toPNG());
  };
  const memoryBytes=await fs.readFile(path.join(fixture.memory,'Projects/synthetic.md'));
  try {
    Object.assign(instances,{home:fixture.home,base:fixture.base,services:new NativeServices({home:fixture.home}),records:[],environment:{}});
    backend.settingsRuntime={...runtime,env:{...runtime.env,HOME:fixture.home}};
    backend.setupHomes=new (require('./setup-homes.cjs').SetupHomes)(path.join(fixture.base,'Companion'));
    const inventory=await instances.refresh();assert.equal(inventory.instances.length,1);const id=inventory.instances[0].id;
    await evaluate("state.active=false;state.daemon=null;state.daemonDraft=null;state.error='';state.page='instances';refreshInstances()");
    await wait('!state.instanceBusy');
    assert(await evaluate("!!document.querySelector('[data-removal=detach]')"));
    // Exercise a genuine Python refusal through the packaged IPC/renderer path.
    // Only the disposable task definition is changed and restored byte-for-byte.
    const night=(await instances.services.list()).items.find(item=>item.label.endsWith('.dreaming'));
    assert(night);
    const exactNight=await fs.readFile(night.file),plist=require('plist');
    try {
      const altered=plist.parse(exactNight.toString());altered.WorkingDirectory=root;
      await fs.writeFile(night.file,plist.build(altered));
      await evaluate(`openRemoval(${JSON.stringify(id)},'uninstall')`);await wait('!removalState.busy');
      assert(await evaluate("removalState.failure?.diagnostic.phase==='review' && !removalState.plan"));
      assert(await evaluate("!!document.querySelector('[data-action=removal-export]') && !document.querySelector('[data-action=removal-confirm]')"));
      const exported=await evaluate(`window.companion.exportDiagnostic('removal',${JSON.stringify(id)})`);
      assert.equal(exported.report.schema,'daemon.removal-diagnostic.v1');
      assert.equal(exported.report.code,'services');
      assert(!JSON.stringify(exported.report).includes(fixture.base));
      for(const language of ['en','fr']) {
        await evaluate(`changeLanguage('${language}')`);await wait(`document.documentElement.lang==='${language}' && !languageSaving`);
        await captureRemoval(language+'-removal-diagnostic.png');
      }
      assert.deepEqual(await fs.readFile(path.join(fixture.memory,'Projects/synthetic.md')),memoryBytes);
      assert.equal((await instances.services.list()).items.length,4);
    }finally{await fs.writeFile(night.file,exactNight);}
    // Synthetic Windows result at the backend boundary; the native command entry
    // has a separate red/green test. Exercise real main/preload/renderer/export.
    const realSettings=backend.daemonSettings;
    try {
      backend.daemonSettings=async()=>{const e=new Error('removal.services');e.removalDiagnostic={stage:'task_xml',exit_code:1,hresult:-2147024891,privateValue:'never-export-this'};throw e;};
      await evaluate(`openRemoval(${JSON.stringify(id)},'uninstall')`);await wait('!removalState.busy');
      assert(await evaluate("removalState.failure.diagnostic.stage==='task_xml' && removalState.failure.diagnostic.hresult===-2147024891 && removalState.failure.diagnostic.exit_code===1"));
      const report=await evaluate(`window.companion.exportDiagnostic('removal',${JSON.stringify(id)})`);
      assert.equal(report.report.hresult,-2147024891);
      assert(!JSON.stringify(report).includes('never-export-this'));
      await captureRemoval('fr-windows-error-details-synthetic.png');
    }finally{backend.daemonSettings=realSettings;}
    await evaluate("action('removal-retry')");await wait('!removalState.busy');
    assert(await evaluate("!!removalState.plan && !removalState.failure"));
    await evaluate("action('removal-back')");await wait('!state.instanceBusy');

    await evaluate("document.querySelector('.removal-options').open=true;changeLanguage('fr')");await wait("document.documentElement.lang==='fr'");
    assert(await evaluate("document.body.textContent.includes('Désinstaller Hermes Agent et le daemon') && document.body.textContent.includes('Retirer le daemon, garder Hermes Agent') && !document.body.textContent.includes('Détacher ou désinstaller')"));
    await evaluate("document.querySelector('.removal-options').open=true;document.querySelector('.removal-options').scrollIntoView({block:'center'})");
    await captureRemoval('fr-removal-choices.png');
    assert(await evaluate("document.querySelector('.removal-options').open"));
    // Full uninstall describes both programs and binds the installation name.
    await evaluate(`openRemoval(${JSON.stringify(id)},'uninstall')`);await wait('!removalState.busy');
    assert(await evaluate("removalState.plan.daemonAttached && removalState.plan.confirmationText==='Hermes - Nova'"));
    assert(await evaluate("document.querySelector('h1').textContent==='Désinstaller Hermes Agent et le daemon' && document.body.textContent.includes('ne répondra plus sur Telegram')"));
    await evaluate("document.querySelector('#removal-name').value='Nova';document.querySelector('#removal-name').dispatchEvent(new Event('input'))");
    assert(await evaluate("document.querySelector('[data-action=removal-confirm]').disabled"));
    await evaluate("document.querySelector('#removal-name').value='Hermes - Nova';document.querySelector('#removal-name').dispatchEvent(new Event('input'))");
    assert(await evaluate("!document.querySelector('[data-action=removal-confirm]').disabled"));
    await captureRemoval('fr-removal-uninstall.png');
    await evaluate("action('removal-back')");await wait('!state.instanceBusy');
    // Review/cancel is read-only; wrong name cannot submit.
    await evaluate(`openRemoval(${JSON.stringify(id)},'detach')`);await wait('!removalState.busy');
    assert(await evaluate("!!removalState.plan && !state.error"),await evaluate('state.error'));
    assert(await evaluate("document.querySelector('[data-action=removal-confirm]').disabled"));
    await evaluate("document.querySelector('#removal-name').value='nova';document.querySelector('#removal-name').dispatchEvent(new Event('input'))");
    assert(await evaluate("document.querySelector('[data-action=removal-confirm]').disabled"));
    for(const language of ['en','fr'])for(const theme of ['light','dark']) {
      await evaluate(`changeLanguage(${JSON.stringify(language)})`);await wait(`document.documentElement.lang===${JSON.stringify(language)}`);
      await evaluate(`changeTheme(${JSON.stringify(theme)})`);await wait('!themeSaving');
      assert(await evaluate("document.querySelector('#removal-name').value==='nova'"));
      if(language==='fr')assert(await evaluate("document.body.textContent.includes('Hermes Agent reste installé') && document.body.textContent.includes('n’utilisera plus la mémoire Daemons') && !document.body.textContent.includes('Hermes stays installed')"));
      await evaluate("document.getAnimations().filter(a=>a.effect?.getTiming().iterations!==Infinity).forEach(a=>a.finish())");
      await layout(window);await contrast(window);
      await captureRemoval(`${language}-${theme}-removal-review.png`);
    }
    await evaluate("document.querySelector('#removal-name').scrollIntoView({block:'center'});document.querySelector('#removal-name').value='Nova';document.querySelector('#removal-name').dispatchEvent(new Event('input'))");
    await captureRemoval('fr-dark-removal-confirm.png');
    await evaluate("action('removal-back')");assert((await instances.services.list()).items.length===4);
    await evaluate(`openRemoval(${JSON.stringify(id)},'detach')`);await wait('!removalState.busy');
    assert.equal(await evaluate('window.companion.removalFolder(removalState.plan.id,removalState.plan.token,"memory",true)'),fixture.memory);
    await evaluate("document.querySelector('#removal-name').value='Nova';document.querySelector('#removal-name').dispatchEvent(new Event('input'));document.querySelector('[data-action=removal-confirm]').click();void 0");
    await wait('!removalState.busy');assert(await evaluate('removalState.plan?.done && !state.error'),await evaluate('state.error'));
    assert.equal((await instances.services.list()).items.length,1);
    await evaluate("action('removal-back')");await wait('!state.instanceBusy');
    assert(await evaluate("!state.inventory.instances[0].daemon"));
    assert(await evaluate("!document.querySelector('[data-removal=detach]')"));
    await evaluate(`openRemoval(${JSON.stringify(id)},'uninstall')`);await wait('!removalState.busy');
    assert(await evaluate('!!removalState.plan && !state.error'),await evaluate('state.error'));
    assert(await evaluate("!removalState.plan.daemonAttached && document.querySelector('h1').textContent==='Désinstaller Hermes Agent'"));
    // Exercise an apply-phase cleanup failure after detachment through real IPC.
    const realRemovalSettings=backend.daemonSettings;
    try {
      backend.daemonSettings=async function(request,...args){
        if(request.operation==='apply'){const e=new Error('removal.recovery');e.removalDiagnostic={stage:'file_open',winerror:32,errno:13,privateValue:'never-export-this'};throw e;}
        return realRemovalSettings.call(this,request,...args);
      };
      await evaluate("document.querySelector('#removal-name').value='Hermes - Nova';document.querySelector('#removal-name').dispatchEvent(new Event('input'));action('removal-confirm');void 0");
      await wait('!removalState.busy');
      assert(await evaluate("removalState.failure?.diagnostic.phase==='apply' && removalState.failure.diagnostic.stage==='file_open' && removalState.failure.diagnostic.winerror===32"));
      const report=await evaluate(`window.companion.exportDiagnostic('removal',${JSON.stringify(id)})`);
      assert.equal(report.report.errno,13);assert(!JSON.stringify(report).includes('never-export-this'));
      await captureRemoval('fr-windows-cleanup-recovery-synthetic.png');
      assert.deepEqual(await fs.readFile(path.join(fixture.memory,'Projects/synthetic.md')),memoryBytes);
    }finally{backend.daemonSettings=realRemovalSettings;}
    await evaluate("action('removal-retry')");await wait('!removalState.busy');
    assert(await evaluate('!!removalState.plan && !removalState.failure'));
    // Deleting the memory is opt-in, warned on the page and confirmed again
    // natively before anything starts. A synthetic trash replaces the real one.
    const {dialog}=require('electron'),originalDialog=dialog.showMessageBox,trash=path.join(root,'trash');
    const prompts=[];let answer=0;
    try {
      dialog.showMessageBox=async(_parent,options)=>{prompts.push(options);assert.equal(options.defaultId,0);assert.equal(options.cancelId,0);return {response:answer};};
      backend.smokeTrash=async target=>{await fs.rename(target,trash);};
      assert(await evaluate("removalState.plan.memoryDeletable && !removalState.deleteMemory && document.body.textContent.includes('Supprimer aussi la mémoire de ce daemon')"));
      await evaluate("document.querySelector('[data-removal-memory]').click()");
      assert(await evaluate("removalState.deleteMemory && document.body.textContent.includes('Votre mémoire sera placée dans la Corbeille.') && document.body.textContent.includes('3 fichiers') && document.querySelector('[data-action=removal-confirm]').textContent==='Désinstaller et supprimer la mémoire'"));
      await evaluate("document.querySelector('#removal-name').value='Hermes - Nova';document.querySelector('#removal-name').dispatchEvent(new Event('input'));document.querySelector('.removal-memory').scrollIntoView({block:'center'})");
      await captureRemoval('fr-removal-delete-memory.png');
      // Cancelling the second confirmation changes nothing at all.
      await evaluate("action('removal-confirm');void 0");await wait('!removalState.busy');
      assert.equal(prompts.length,1);assert(prompts[0].message.includes('Nova')&&prompts[0].detail.includes(fixture.memory)&&prompts[0].detail.includes('Corbeille'));
      assert(await evaluate('!!removalState.plan && !removalState.plan.done && !state.error'));
      assert.equal((await instances.services.list()).items.length,1);
      assert.deepEqual(await fs.readFile(path.join(fixture.memory,'Projects/synthetic.md')),memoryBytes);
      answer=1;
      await evaluate("action('removal-confirm');void 0");await wait('!removalState.busy');
      assert(await evaluate('removalState.plan?.done && removalState.plan.memoryRemoved===true && !state.error'),await evaluate('state.error'));
    } finally {dialog.showMessageBox=originalDialog;delete backend.smokeTrash;}
    assert.equal(prompts.length,2);
    assert.equal((await instances.services.list()).items.length,0);
    assert.equal((await instances.refresh()).instances.length,0);
    await assert.rejects(fs.stat(fixture.memory));
    assert.deepEqual(await fs.readFile(path.join(trash,'Projects/synthetic.md')),memoryBytes);
    assert(await evaluate("document.body.textContent.includes('Mémoire placée dans la Corbeille.') && !document.querySelector('[data-action=removal-memory]')"));
    await captureRemoval('removal-complete.png');
    return {status:'passed',elapsed_ms:Math.round(performance.now()-started),real_packaged_ipc:true,real_python_engine:true,native_launchd:true,typed_confirmation:true,installation_name_confirmation:true,explicit_consequences:true,detached_host_labels:true,cancel_preserved:true,bilingual:true,detach_then_uninstall:true,memory_retained_until_chosen:true,memory_deletion_confirmed_twice:true,memory_to_trash:true,personal_services_changed:0};
  } finally {
    await invoke(['stop',metadata]);
    Object.assign(instances,prior);backend.settingsRuntime=priorRuntime;backend.setupHomes=priorHomes;
    await evaluate("removalState.plan=null;removalState.busy=false;state.error='';state.page='instances';refreshInstances()");
  }
}

async function discardTrial(window,backend,instances,output) {
  if(process.platform!=='darwin')return {status:'not-qualified',reason:'Native Windows qualification required'};
  const assert=require('node:assert/strict');
  const exec=require('node:util').promisify(require('node:child_process').execFile);
  const root=path.join(output,'discard-trial');await fs.mkdir(root);
  const fixtureFile=path.join(root,'fixture.py');await fs.writeFile(fixtureFile,await fs.readFile(path.join(__dirname,'removal-fixture.py')));
  const runtime=backend.smokeRuntime;
  const invoke=async args=>JSON.parse((await exec(runtime.python,['-I','-B','-c','import sys,runpy;sys.path.insert(0,sys.argv.pop(1));sys.path.insert(1,sys.argv.pop(1));p=sys.argv.pop(1);runpy.run_path(p,run_name="__main__")',path.join(backend.packageRoot,'payload/src'),runtime.libraries,fixtureFile,...args],{env:runtime.env,timeout:60000})).stdout);
  const fixture=await invoke(['create',path.join(root,'owned')]);
  const metadata=path.join(root,'metadata.json');await fs.writeFile(metadata,JSON.stringify(fixture));
  const attempt=path.dirname(fixture.profile),setup=path.resolve(attempt,'../../../..');
  const prior={home:instances.home,base:instances.base,services:instances.services,records:instances.records,environment:instances.environment};
  const priorRuntime=backend.settingsRuntime,priorHomes=backend.setupHomes;
  const evaluate=code=>window.webContents.executeJavaScript(code);
  const wait=async expr=>{for(let i=0;i<800;i++){if(await evaluate(expr))return;await new Promise(r=>setTimeout(r,50));}throw new Error('Discard UI: '+expr+' '+await evaluate('state.error'));};
  const started=performance.now();
  const memory=await fs.readFile(path.join(fixture.memory,'Projects/synthetic.md'));
  try {
    // Reproduce the boundary after service registration but before commit.
    await fs.unlink(path.join(attempt,'daemon-setup.json'));
    for(const entry of await fs.readdir(path.join(attempt,'daemon/transactions')))await fs.writeFile(path.join(attempt,'daemon/transactions',entry,'journal.json'),'{"status":"applying"}');
    Object.assign(instances,{home:fixture.home,base:fixture.base,services:new (require('./instances.cjs').NativeServices)({home:fixture.home}),records:[],environment:{}});
    backend.settingsRuntime={...runtime,env:{...runtime.env,HOME:fixture.home}};
    backend.setupHomes=new (require('./setup-homes.cjs').SetupHomes)(path.join(fixture.base,'Companion'));
    const setupId=(await backend.setupHomes.list())[0].id;
    await evaluate("state.active=false;state.error='';state.page='instances';refreshInstances()");await wait('!state.instanceBusy');
    assert(await evaluate('!!document.querySelector("[data-setup-discard]")'));
    await evaluate('document.querySelector("[data-setup-discard]").click()');await wait('!discardState.busy');
    assert(await evaluate('!!discardState.plan && !state.error'),await evaluate('state.error'));
    assert.equal((await instances.services.list()).items.length,4);
    await evaluate("action('discard-back')");await wait('!state.instanceBusy');
    assert.equal((await instances.services.list()).items.length,4);
    await evaluate(`openDiscard(${JSON.stringify(setupId)})`);await wait('!discardState.busy');
    for(const lang of ['en','fr'])for(const theme of ['light','dark']) {
      await evaluate(`changeLanguage(${JSON.stringify(lang)});changeTheme(${JSON.stringify(theme)})`);await wait('!themeSaving');
      await layout(window);await contrast(window);
      await new Promise(r=>setTimeout(r,220));
      await fs.writeFile(path.join(output,`${lang}-${theme}-discard-review.png`),(await window.webContents.capturePage()).toPNG());
    }
    // A genuine filesystem failure must leave a retry card and a safe diagnostic.
    await fs.chmod(path.join(attempt,'source'),0o500);
    await evaluate("action('discard-confirm');void 0");await wait('!discardState.busy');
    assert(await evaluate('!!state.error && !discardState.plan'),await evaluate('state.error'));
    assert.equal(await backend.setupHomes.summary(setup),'discarding');
    const diagnostic=await evaluate(`window.companion.diagnostic(${JSON.stringify(setupId)})`);
    assert.equal(diagnostic.status,'failed');assert(diagnostic.events.some(e=>e.operation==='discard'&&e.status==='failed'));
    assert(!JSON.stringify(diagnostic).includes(setup));
    await fs.chmod(path.join(attempt,'source'),0o700);
    await evaluate("action('discard-back')");await wait('!state.instanceBusy');
    assert(await evaluate('!!document.querySelector("[data-setup-discard]") && !document.querySelector("[data-setup-resume]")'));
    await evaluate(`openDiscard(${JSON.stringify(setupId)})`);await wait('!discardState.busy');
    assert(await evaluate('!!discardState.plan && !state.error'),await evaluate('state.error'));
    await evaluate("action('discard-confirm');void 0");await wait('!discardState.busy');
    assert(await evaluate('discardState.plan?.done && !state.error'),await evaluate('state.error'));
    assert.equal((await instances.services.list()).items.length,0);
    assert.equal((await instances.refresh()).instances.length,0);
    assert.deepEqual(await fs.readFile(path.join(fixture.memory,'Projects/synthetic.md')),memory);
    await evaluate("action('discard-back')");await window.webContents.reload();await wait('!!state.inventory && !state.instanceBusy');
    assert(await evaluate('state.inventory.setups.length===0 && !document.querySelector("[data-setup-discard]") && !document.querySelector("[data-setup-resume]")'));
    return {status:'passed',elapsed_ms:Math.round(performance.now()-started),real_packaged_ipc:true,real_python_engine:true,native_launchd_jobs_removed:4,cancel_preserved:true,bilingual:true,filesystem_failure_and_retry:true,diagnostic_without_private_paths:true,memory_exact:true,reload_removed_card:true,personal_services_changed:0};
  } finally {
    await fs.chmod(path.join(attempt,'source'),0o700).catch(()=>{});
    await invoke(['stop',metadata]);Object.assign(instances,prior);backend.settingsRuntime=priorRuntime;backend.setupHomes=priorHomes;
    await evaluate("discardState.plan=null;discardState.busy=false;state.error='';state.page='instances';refreshInstances()");
  }
}

// Real renderer/preload/IPC/coordinator, with a synthetic review boundary only.
// Native service activation is qualified separately by the launchd harness.
async function hostAdditionTrial(window,backend,instances,output) {
  const old={refresh:instances.refresh,start:backend.start};
  const source={id:'synthetic-source',host:'hermes',profile:path.join(instances.base,'source/profile'),state:'running',daemon:{name:'Aster',runtime:path.join(instances.base,'shared/memory/.daemon')}};
  const inventory={instances:[source],servicesAvailable:true};
  const evaluate=code=>window.webContents.executeJavaScript(code);
  const wait=async code=>{for(let i=0;i<200;i++){if(await evaluate(code))return;await new Promise(r=>setTimeout(r,50));}throw new Error('Host addition UI: '+code);};
  const requests=[];
  try {
    instances.refresh=async()=>inventory;
    // The guided session is the real protocol; its Python side is covered by its own tests.
    backend.start=async(mode,setupId,request)=>{
      requests.push({mode,setupId,request});
      const send=frame=>backend.event({schema:'daemon.desktop.v1',...frame});
      backend.active=true;
      setTimeout(()=>{send({type:'phase',name:'host'});send({type:'message',message:'Aster will also answer you from OpenClaw, through a second Telegram bot.'});
        send({type:'prompt',id:1,kind:'choice',title:'Add OpenClaw?',options:['Continue','Cancel'],default:0,guidance:['Aster will also answer you from OpenClaw, through a second Telegram bot.']});},50);
      return {started:true};
    };
    for(const language of ['en','fr']){
      await evaluate(`changeLanguage(${JSON.stringify(language)})`);
      await wait(`document.documentElement.lang===${JSON.stringify(language)}`);
      await evaluate("action('instances')");await wait("!state.instanceBusy && !!document.querySelector('[data-host-add]')");
      await evaluate("document.querySelector('[data-host-add]').click()");
      await wait("state.page==='setup' && state.mode==='add-host' && state.prompt?.id===1");
      if(!await evaluate("document.querySelectorAll('.steps span').length===5 && !!document.querySelector('[data-action=host-diagnostic]') && document.querySelector('h1').textContent===(language==='fr'?'Ajouter OpenClaw à votre daemon.':'Add OpenClaw to your daemon.') && !document.body.textContent.includes('/source/profile')"))
        throw new Error('Add a host must use the guided setup page with its own steps and no private path');
      await evaluate("document.getAnimations().filter(a=>a.effect?.getTiming().iterations!==Infinity).forEach(a=>a.finish())");
      await evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');
      await layout(window);await contrast(window);
      await fs.writeFile(path.join(output,'host-add-start-'+language+'.png'),(await window.webContents.capturePage()).toPNG());
      backend.event({schema:'daemon.desktop.v1',type:'finished',result:'cancelled',message:'OpenClaw setup stopped.'});backend.active=false;backend.event({type:'idle'});
      await wait("!state.active");
    }
    const expected={source_profile:source.profile,base:instances.base,name:'Aster'};
    if(requests.length!==2||requests.some(r=>r.mode!=='add-host'||JSON.stringify(r.request)!==JSON.stringify(expected)))throw new Error('Add a host must start one guided session with the owned daemon only');
    return {english_french_start:true,guided_session:true,owned_request_only:true,native_activation:false};
  }finally{
    instances.refresh=old.refresh;backend.start=old.start;backend.active=false;
    await evaluate("action('instances')");await wait("!state.instanceBusy");
  }
}

// Delayed native boundary: real renderer, preload and IPC, no personal browser.
async function hostInterfaceFeedbackTrial(window,backend,output) {
 const assert=require('node:assert/strict');
 const evaluate=code=>window.webContents.executeJavaScript(code);
 const wait=async code=>{for(let i=0;i<200;i++){if(await evaluate(code))return;await new Promise(r=>setTimeout(r,25));}throw new Error('Host interface feedback: '+code);};
 let resolve,reject,calls=0;
 backend.smokeOpenHostInterface=async()=>{calls++;return new Promise((yes,no)=>{resolve=yes;reject=no;});};
 try {
  for(const language of ['en','fr']) {
   await evaluate(`changeLanguage(${JSON.stringify(language)})`);
   await evaluate("action('instances')");await wait('!state.instanceBusy');
   await evaluate("document.querySelector('[data-instance-card]').open=true;document.querySelector('[data-host-interface]').scrollIntoView({block:'center'});document.querySelector('[data-host-interface]').click()");
   await wait('hostInterfaceState.busy');
   await new Promise(r=>setTimeout(r,300));
   const before=await evaluate("document.querySelector('.content').scrollTop");
   assert(await evaluate("document.querySelector('[data-instance-card]').open && document.querySelector('[data-host-interface] .spinner') && document.querySelectorAll('[data-host-interface][aria-busy=true]').length===1 && document.querySelectorAll('.host-interface-status').length===1"));
   await evaluate('render()');
   assert(await evaluate("document.querySelector('[data-instance-card]').open"));
   assert.equal(await evaluate("document.querySelector('.content').scrollTop"),before);
   await evaluate("document.getAnimations().filter(a=>a.effect?.getTiming().iterations!==Infinity).forEach(a=>a.finish())");
   await evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');
   await fs.writeFile(path.join(output,language+'-host-interface-opening.png'),(await window.webContents.capturePage()).toPNG());
   resolve({opened:true});await wait('!hostInterfaceState.busy');
   assert(await evaluate("document.querySelector('[data-instance-card]').open && hostInterfaceState.message==='hostInterface.opened' && !document.querySelector('[data-host-interface]').disabled"));
   await evaluate("document.querySelector('[data-host-interface]').click()");await wait('hostInterfaceState.busy');
   reject(new Error('hostInterface.timeout'));await wait('!hostInterfaceState.busy');
   assert(await evaluate("document.querySelector('[data-instance-card]').open && hostInterfaceState.message==='hostInterface.timeout' && document.querySelectorAll('.host-interface-status').length===1"));
   await evaluate("document.querySelector('[data-instance-card]').open=false;render()");
   assert(await evaluate("!document.querySelector('[data-instance-card]').open"));
  }
  assert.equal(calls,4);
  return {passed:true,bilingual:true,delayed_ipc:true,scroll_preserved:true,success_and_error_visible:true,other_cards_unchanged:true};
 } finally {delete backend.smokeOpenHostInterface;await evaluate("Object.assign(hostInterfaceState,{id:null,busy:false,message:''});render()");}
}

async function assertRejectAttachment(backend,source,base) {
 const assert=require('node:assert/strict');
 await assert.rejects(backend.daemonSettings({operation:'create-start',source_profile:source,base,installation_id:'c'.repeat(32),port:19001},'attachment'),/attachment.source-profile/);
 const report=backend.hostDiagnostic(source).read();
 assert(report.events.some(e=>e.attachment_stage==='source-profile'&&e.exception_type==='ValueError'&&e.status==='failed'));
 assert(!JSON.stringify(report).includes(source));
}

// Back up the Hermes fixture through the packaged IPC, the real Python engine
// and real encryption, then open it for a restore without running a setup.
async function backupTrial(window,backend,instances,fixture,output) {
  const started=performance.now();
  const assert=require('node:assert/strict');
  const crypto=require('./backup-crypto.cjs');
  const evaluate=code=>window.webContents.executeJavaScript(code);
  const wait=async expression=>{for(let i=0;i<400;i++){if(await evaluate(expression))return;await new Promise(r=>setTimeout(r,50));}throw new Error('Backup: '+expression+' '+await evaluate('state.error'));};
  const capture=async name=>{await new Promise(r=>setTimeout(r,150));await layout(window);await contrast(window);
    assert(await evaluate('document.documentElement.scrollWidth<=innerWidth'));
    await fs.writeFile(path.join(output,name+'.png'),(await window.webContents.capturePage()).toPNG());};
  // A Companion-shaped vault: layout markers and a per-user voice, removed afterwards.
  const internal=path.join(fixture.memory,'.daemon');
  const added={[path.join(internal,'vault-layout.json')]:JSON.stringify({schema:1,layout:'root-folders-v1',vault_root:fixture.memory,runtime_root:internal})+'\n',
    [path.join(internal,'.memory-structure-prototype.json')]:JSON.stringify({schema:1,memory_root:internal})+'\n',
    [path.join(internal,'users/12345/persona.md')]:'Synthetic persona\n'};
  const usersExisted=await fs.access(path.join(internal,'users')).then(()=>true,()=>false);
  for(const [file,text] of Object.entries(added)){await fs.mkdir(path.dirname(file),{recursive:true});await fs.writeFile(file,text);}
  const before=new Map();
  async function collect(root,map){for(const e of await fs.readdir(root,{withFileTypes:true})){const f=path.join(root,e.name);if(e.isDirectory())await collect(f,map);else if(e.isFile())map.set(f,(await fs.readFile(f)).toString('base64'));}return map;}
  await collect(fixture.memory,before);
  const savedRuntime=backend.settingsRuntime,realStart=backend.start;
  backend.settingsRuntime=backend.smokeRuntime;
  let restoreCall=null;
  try {
    const inventory=await instances.refresh();const hermes=inventory.instances.find(r=>r.profile===fixture.hermes);
    await evaluate("changeLanguage('en')");await wait("document.documentElement.lang==='en'");
    await evaluate(`openDaemon(${JSON.stringify(hermes.id)})`);await wait('!!state.daemon && !state.daemonBusy');
    await evaluate("document.querySelector('[data-action=daemon-backup]').click()");await wait("state.page==='backup'");
    await capture('en-backup-form');
    await evaluate("document.querySelector('#backup-password').value='synthetic-backup-password';document.querySelector('#backup-confirm').value='other-password-123';document.querySelector('[data-action=backup-confirm]').click()");
    await wait("state.error==='backup.passwordMismatch'");
    assert(await evaluate("document.querySelector('.error').textContent===locale.t('backup.passwordMismatch',language)"));
    await evaluate("document.querySelector('#backup-confirm').value='synthetic-backup-password';document.querySelector('[data-action=backup-confirm]').click()");
    await wait("!!backupState.result && !backupState.busy");
    const result=await evaluate('backupState.result');
    // Identity, per-user voice and the visible note; layout markers are rebuilt on restore.
    assert(result.saved && result.files===3 && result.failedTurns===0 && !(await evaluate("document.querySelector('#backup-password')")),JSON.stringify(result));
    await capture('en-backup-saved');
    assert.deepEqual(await collect(fixture.memory,new Map()),before);
    // The file is encrypted: the plain archive appears only through the password.
    const raw=await fs.readFile(result.path);assert(raw.subarray(0,crypto.MAGIC.length).equals(crypto.MAGIC)&&!raw.includes(Buffer.from('Synthetic fact')));
    await evaluate("changeLanguage('fr')");await wait("document.documentElement.lang==='fr'");await capture('fr-backup-saved');
    // Restore: the wrong password is refused before any setup; the right one starts it.
    backend.start=async(mode,setupId,hostAddition,archive)=>{restoreCall={mode,setupId,hostAddition,inside:path.dirname(archive),bytes:await fs.readFile(archive)};return {started:true};};
    await evaluate("state.page='instances';render()");
    assert(await evaluate("!!document.querySelector('[data-action=restore-choose]')"));
    await evaluate(`window.companion.chooseBackup(${JSON.stringify(result.path)}).then(c=>{Object.assign(restoreState,{file:c.file,busy:false,password:''});state.page='restore';render();})`);
    await wait("state.page==='restore' && !!document.querySelector('#restore-password')");
    await capture('fr-restore-form');
    await evaluate("document.querySelector('#restore-password').value='wrong-password-123';document.querySelector('[data-action=restore-confirm]').click()");
    await wait("state.error.includes('backup.authenticationFailed') && !restoreState.busy");
    assert.equal(restoreCall,null);
    await evaluate("document.querySelector('#restore-password').value='synthetic-backup-password';document.querySelector('[data-action=restore-confirm]').click()");
    await wait("state.page==='setup' && state.active");
    assert(restoreCall&&restoreCall.mode==='new'&&restoreCall.setupId===null&&restoreCall.hostAddition===null);
    assert(restoreCall.bytes.subarray(0,2).equals(Buffer.from('PK')));
    // The private decryption folder is removed once setup has taken the archive.
    await assert.rejects(fs.access(restoreCall.inside));
    await evaluate("state.active=false;state.page='instances';changeLanguage('en')");await wait("document.documentElement.lang==='en'");
    const drive=await driveTrial({window,backend,instances,hermes,output,evaluate,wait,capture,assert,setStart:value=>{backend.start=value;}});
    return {status:'passed',elapsed_ms:Math.round(performance.now()-started),real_packaged_ipc:true,real_python_engine:true,encrypted_file:true,
      password_mismatch_refused:true,wrong_password_refused_before_setup:true,memory_unchanged:true,restore_started:true,bilingual:true,model_calls:0,drive};
  } finally {
    backend.start=realStart;backend.settingsRuntime=savedRuntime;
    for(const file of Object.keys(added))await fs.rm(file,{force:true});
    if(!usersExisted)await fs.rm(path.join(internal,'users'),{recursive:true,force:true});
  }
}

// Google, Drive and update requests leave from the main process while the
// window's session cancels everything but the app's files. A loopback server
// stands in for Google: the request must reach it.
async function networkTrial(backend) {
  const http=require('node:http');let received=0;
  const server=http.createServer((request,response)=>{received++;response.writeHead(200,{'Content-Type':'application/json'});response.end('{"ok":true}');});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  try {
    const response=await backend.networkFetch(`http://127.0.0.1:${server.address().port}/token`,{method:'POST',redirect:'error',body:new URLSearchParams({grant_type:'authorization_code'})});
    if(response.status!==200||!(await response.json()).ok||received!==1)throw new Error('Main-process request did not reach the server');
  } finally {server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
  return {status:'passed',main_process_requests_unblocked:true,drive_upload:await driveUploadTrial(backend)};
}

// The real Drive client, through the app's real network session, against a
// loopback stand-in for Google's resumable upload API. Chromium refuses some
// requests a plain fetch accepts, so the synthetic Drive of driveTrial cannot
// show them.
async function driveUploadTrial(backend) {
  const http=require('node:http'),crypto=require('node:crypto');const {Drive,MARKER}=require('./backup-drive.cjs');
  const work=await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(),'daemons-drive-upload-')));
  const plain=path.join(work,'plain.zip'),file=path.join(work,'memory.daemonbackup');
  await fs.writeFile(plain,crypto.randomBytes(9*1024*1024));await require('./backup-crypto.cjs').encrypt(plain,file,'synthetic-password-1');
  let stored=Buffer.alloc(0),metadata=null,chunks=0,probes=0;
  const server=http.createServer((request,response)=>{const parts=[];request.on('data',c=>parts.push(c));request.on('end',()=>{
    const url=new URL(request.url,'http://local'),body=Buffer.concat(parts),json=value=>{response.writeHead(200,{'Content-Type':'application/json'});response.end(JSON.stringify(value));};
    if(request.headers.authorization!=='Bearer synthetic')return response.writeHead(401).end();
    if(url.pathname==='/drive/v3/files'&&request.method==='GET')return json({files:[]});
    if(url.pathname==='/drive/v3/files'&&request.method==='POST')return json({id:'folder'+Math.random().toString(36).slice(2,8)});
    if(url.pathname==='/drive/v3/files/generateIds')return json({ids:['backup1']});
    if(url.pathname==='/upload/drive/v3/files'&&request.method==='POST'){metadata={...JSON.parse(body),size:request.headers['x-upload-content-length']};response.writeHead(200,{location:'https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=s1'});return response.end();}
    if(url.pathname==='/upload/drive/v3/files'&&request.method==='PUT'){
      const range=request.headers['content-range'],total=Number(range.split('/')[1]);
      if(range.startsWith('bytes */'))probes++;else{chunks++;if(Number(request.headers['content-length'])!==body.length)return response.writeHead(400).end();stored=Buffer.concat([stored,body]);}
      if(stored.length===total)return json({id:'backup1'});
      response.writeHead(308,stored.length?{range:'bytes=0-'+(stored.length-1)}:{});return response.end();
    }
    if(url.pathname==='/drive/v3/files/backup1')return json({id:'backup1',name:metadata.name,size:String(stored.length),md5Checksum:crypto.createHash('md5').update(stored).digest('hex'),createdTime:new Date().toISOString(),appProperties:metadata.appProperties,mimeType:'application/octet-stream'});
    response.writeHead(404).end();
  });});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const local=`http://127.0.0.1:${server.address().port}`;
  try {
    const drive=new Drive({token:async()=>'synthetic',fetchImpl:(url,options)=>backend.networkFetch(String(url).replace('https://www.googleapis.com',local),options),sleep:async()=>{}});
    const result=await drive.upload(file,{label:{kind:'manual',daemon:'abc123',name:'Nova'}});
    if(result.id!=='backup1'||!stored.equals(await fs.readFile(file))||chunks!==2||metadata.appProperties.daemons_format!==MARKER)throw new Error('Drive upload through the app network did not complete');
    return {status:'passed',chunks,probes,bytes:stored.length,real_network_session:true};
  } finally {server.closeAllConnections();await new Promise(resolve=>server.close(resolve));await fs.rm(work,{recursive:true,force:true});}
}

// Google Drive, daily backup, background settings and a move, against a
// synthetic Drive and protected store: no Google account, no Keychain entry.
async function driveTrial({window,backend,instances,hermes,output,evaluate,wait,capture,assert,setStart}) {
  const crypto=require('node:crypto');const {Backups}=require('./backup-service.cjs');const backupCrypto=require('./backup-crypto.cjs');
  const key=crypto.randomBytes(32);
  const safeStorage={isEncryptionAvailable:()=>true,
    encryptString:v=>{const iv=crypto.randomBytes(12),c=crypto.createCipheriv('aes-256-gcm',key,iv);return Buffer.concat([iv,c.update(v,'utf8'),c.final(),c.getAuthTag()]);},
    decryptString:b=>{const c=crypto.createDecipheriv('aes-256-gcm',key,b.subarray(0,12));c.setAuthTag(b.subarray(-16));return Buffer.concat([c.update(b.subarray(12,-16)),c.final()]).toString('utf8');}};
  const files=[];
  class SyntheticDrive {
    async upload(file,{label}){const bytes=await fs.readFile(file);files.unshift({id:'synthetic'+files.length,createdAt:new Date().toISOString(),bytes:bytes.length,kind:label.kind,daemon:label.daemon,name:label.name,content:bytes});return {bytes:bytes.length};}
    async backups(){return files.map(({content,...f})=>f);}
    async remove(id){files.splice(files.findIndex(f=>f.id===id),1);}
    async download(id,target){await fs.writeFile(target,files.find(f=>f.id===id).content,{flag:'wx',mode:0o600});}
  }
  class SyntheticOAuth {constructor({store}){this.store=store;} async signIn(){await this.store.put({refresh_token:'synthetic'});return {connected:true};} async token(){return 'synthetic';} async disconnect(){await this.store.delete();}}
  backend.smokeBackups(new Backups({userData:path.join(require('electron').app.getPath('userData'),'synthetic-drive'),instances,backend,safeStorage,platform:process.platform,
    oauthConfig:{client_id:'synthetic'},GoogleOAuth:SyntheticOAuth,Drive:SyntheticDrive}));
  const password='synthetic-daily-password';
  // Settings: background mode and Google Drive.
  await evaluate("action('settings')");await wait("state.page==='settings' && !!document.querySelector('[data-google=connect]')");
  assert(await evaluate("!!document.querySelector('[data-background=enabled]') && document.querySelector('[data-background=login]').disabled"));
  await evaluate("document.querySelector('[data-background=enabled]').click()");await wait("backgroundMode.enabled && backgroundMode.openAtLogin && !backgroundMode.busy");
  const saved=await evaluate('window.companion.preferences()');assert(saved.background===true&&saved.openAtLogin===true);
  await evaluate("document.querySelector('[data-background=enabled]').click()");await wait("!backgroundMode.enabled && !backgroundMode.busy");
  await evaluate("document.querySelector('[data-google=connect]').click()");await wait("google.connected && !google.busy");
  await capture('en-settings-drive');
  // Daily backup: turning it on also keeps the app running in the background.
  await evaluate(`openDaemon(${JSON.stringify(hermes.id)})`);await wait('!!state.daemon && !state.daemonBusy && !!daemonBackup');
  assert(await evaluate("!document.querySelector('[data-action=daemon-backup-auto]').disabled"));
  await evaluate("document.querySelector('[data-action=daemon-backup-auto]').click()");await wait("state.page==='backup' && backupState.mode==='automatic'");
  await evaluate(`document.querySelector('#backup-password').value=${JSON.stringify(password)};document.querySelector('#backup-confirm').value=${JSON.stringify(password)};document.querySelector('[data-action=backup-confirm]').click()`);
  await wait("!!backupState.result && !backupState.busy");
  assert(await evaluate("backupState.result.automatic && backupState.result.backgroundEnabled && backgroundMode.enabled"));
  await capture('en-daily-backup-on');
  for(let i=0;i<100&&!files.length;i++)await new Promise(r=>setTimeout(r,100));
  assert.equal(files.length,1);assert.equal(files[0].kind,'automatic');
  // The daily file opens with the password alone.
  const probe=path.join(output,'daily-probe');await fs.writeFile(probe+'.enc',files[0].content);
  await backupCrypto.decrypt(probe+'.enc',probe+'.zip',password);assert((await fs.readFile(probe+'.zip')).subarray(0,2).equals(Buffer.from('PK')));
  await evaluate(`openDaemon(${JSON.stringify(hermes.id)})`);await wait('!!state.daemon && !state.daemonBusy && !!daemonBackup && !!daemonBackup.lastSuccess');
  await evaluate("changeLanguage('fr')");await wait("document.documentElement.lang==='fr'");
  await evaluate("document.querySelector('.backup-help').scrollIntoView()");await capture('fr-daemon-daily-backup');
  // Restore from Drive, as a move.
  let call=null;setStart(async(mode,setupId,hostAddition,archive,move)=>{call={mode,move,bytes:await fs.readFile(archive)};return {started:true};});
  await evaluate("state.page='instances';render();action('restore-choose')");await wait("state.page==='restore' && restoreState.step==='source' && Array.isArray(restoreState.drive) && !restoreState.busy");
  assert.equal(await evaluate('restoreState.drive.length'),1);await capture('fr-restore-drive');
  await evaluate("document.querySelector('[data-restore-drive]').click()");await wait("restoreState.step==='password' && !restoreState.busy");
  await evaluate("document.querySelector('[data-restore-kind][value=move]').click()");await wait("restoreState.move===true");
  await capture('fr-restore-move');
  await evaluate(`document.querySelector('#restore-password').value=${JSON.stringify(password)};document.querySelector('[data-action=restore-confirm]').click()`);
  await wait("state.page==='setup' && state.active");
  assert(call&&call.mode==='new'&&call.move===true&&call.bytes.subarray(0,2).equals(Buffer.from('PK')));
  await evaluate("state.active=false;state.page='instances';changeLanguage('en')");await wait("document.documentElement.lang==='en'");
  await evaluate("window.companion.disconnectGoogle()");
  // The background icons ship and load: the template image on Mac, the .ico on Windows.
  const {nativeImage}=require('electron');
  // Electron reads .ico files on Windows only; the Mac package still carries it.
  await fs.access(path.join(__dirname,'assets','app-icon.ico'));
  const trayIcon=nativeImage.createFromPath(path.join(__dirname,'assets',process.platform==='win32'?'app-icon.ico':'trayTemplate.png'));
  assert(!trayIcon.isEmpty());
  if(process.platform==='darwin')assert.deepEqual(trayIcon.getSize(),{width:16,height:16});
  return {status:'passed',tray_icons_load:true,synthetic_drive:true,background_saved:true,daily_backup_ran:true,daily_opens_with_password:true,drive_list:true,move_requested:true,google_account:false};
}
