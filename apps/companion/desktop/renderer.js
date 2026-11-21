'use strict';
const hostInterfaceState={id:null,busy:false,message:''};
const openHostDetails=new Set();
let obsidianInfo=null,obsidianMessage='',obsidianBusy=false;
// The daemon's model and API key (Settings > Model). The key typed here is sent once and never kept.
let providerInfo=null,providerState={},daemonUpdate=null;
let locale, language = 'en', theme = 'light', appInfo = {};
const navigation = {instances:'My daemons', about:'About'};
const darkAppearance = window.matchMedia('(prefers-color-scheme: dark)');
let draft = null, languageSaving = false, themeSaving = false;
let update = {enabled:false, state:'disabled'}, updateBusy = false;
let renderedPage = null;
let started={steps:null,open:false,loading:false};
const reportState={id:null,health:null,diagnostic:null,service:null,busy:false,message:'',back:'instances',expanded:new Set()};
const discardState={plan:null,busy:false,id:null};
// Passwords stay in these fields until submitted; they are cleared afterwards.
const backupState={id:null,name:'',busy:false,result:null,password:'',confirm:'',mode:'once',destination:'file'};
const restoreState={file:'',busy:false,password:'',step:'password',drive:null,move:false};
// Google Drive: configured in this build, signed in, and this daemon's daily backup.
let google={configured:false,connected:false,busy:false},daemonBackup=null;
// Background mode: the app keeps running from its menu bar or notification area icon.
let backgroundMode={enabled:false,openAtLogin:false,platform:'darwin',busy:false};
async function refreshGoogle(id=null){try{const value=await window.companion.backupStatus(id);google={...google,configured:value.configured,connected:value.connected};if(id!==null)daemonBackup=value.automatic;}catch{/* Drive stays hidden. */}}
// Add a host runs as a guided session; only its daemon id is kept for the report.
const hostAdditionState={id:null};
const removalState={plan:null,busy:false,name:'',message:'',id:null,action:null,failure:null,deleteMemory:false};
const state = { page: 'instances', active: false, phase: 'host', prompt: null, messages: [], guidance: [], links: [], activity: '', error: '', result: null, overview: null, inventory:null, instanceBusy:false, savedSetup:'none', setupStarted:false, operation:null, daemon:null, daemonDraft:null, daemonBusy:false, daemonMessage:'', daemonPronounChoice:null, daemonCustomPronouns:'', daemonPreferencesOpen:null };
// Restart progress per host row, shown on daemon cards (restartWithStatus).
let restarts={},channelTimer=null;
const phases = ['host', 'identity', 'model', 'telegram', 'installation', 'conversation'];
const labels = { host: 'Getting started', identity: 'You and your daemon', model: 'Model access', review: 'Review', telegram: 'Telegram', installation: 'Installation', conversation: 'First conversation' };
const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// Translate the whole line first: emphasis splits it into text nodes that the
// page-wide localizer can no longer match.
const guidanceText = value => {
  const text = locale.message(value, language);
  return locale.isConfirmation(value) || locale.isEmphasis(value) ? `<strong>${escape(text)}</strong>` : escape(text).replace(/(@BotFather|@userinfobot|\/newbot|\/start|ChatGPT connection [12] of 2|Connexion ChatGPT [12] sur 2|Memory connection confirmed \(1 of 2\)|Connexion pour la mémoire confirmée \(1 sur 2\))/g, '<strong>$1</strong>');
};
// A failure that names an outside service also shows what that service's status
// page says (services.js), so a reader can tell its outage from a fault of the app.
const serviceStates = new Map(), serviceRequests = new Set();
function failureService(value) {
  const found = value ? locale.statusOf(value) : null;
  const id = found ? found.id || daemonServices.serviceId(found.name) : null;
  return id && daemonServices.readable(id) ? id : null;
}
function serviceStatusText(id) {
  const name = daemonServices.SERVICES[id].name, value = serviceStates.get(id);
  const t = (key, args = {}) => escape(locale.t(key, language, {service: name, ...args}));
  // The service's own words stay as published, marked so they are never translated.
  const quoted = (key, field, items) => escape(locale.t(key, language, {service: name, [field]: '\u0001'}))
    .replace('\u0001', `<span data-original>${escape(items.join(' · '))}</span>`);
  const text = !value ? t('service.checking')
    : value.state === 'incident' && value.incidents?.length ? quoted('service.incident', 'incident', value.incidents)
    : value.state === 'incident' && value.disruptions?.length ? quoted('service.disruption', 'component', value.disruptions)
    : value.state === 'clear' ? t('service.clear') : t('service.unreachable');
  return `<span>${text}</span> <button class="text-button" data-service-open="${id}">${t('service.open')} ↗</button>`;
}
function requestServiceStatus(id) {
  const known = serviceStates.get(id);
  if ((known && Date.now() - known.at < 60000) || serviceRequests.has(id)) return;
  serviceRequests.add(id);
  window.companion.serviceStatus(id).then(value => ({...value, at: Date.now()}), () => ({state: 'unreachable', at: Date.now()}))
    .then(value => {
      serviceStates.set(id, value); serviceRequests.delete(id);
      document.querySelectorAll(`[data-service-status="${id}"]`).forEach(element => { element.innerHTML = serviceStatusText(id); });
    });
}
// One line per service, under the messages that name it.
const serviceStatus = (...values) => serviceLines(values.map(failureService));
function serviceLines(list) {
  const ids = [...new Set(list.filter(id => id && daemonServices.readable(id)))];
  ids.forEach(requestServiceStatus);
  return ids.map(id => `<span class="service-status" data-service-status="${id}">${serviceStatusText(id)}</span>`).join('');
}
// A failure shown by its code: its English catalog text names the service.
const codeStatus = code => { try { return serviceStatus(locale.t(code, 'en')); } catch { return ''; } };
document.addEventListener('click', event => {
  const link = event.target.closest?.('[data-service-open]');
  if (link) window.companion.openServiceStatus(link.dataset.serviceOpen).catch(() => {});
});
const button = (label, action, primary = false, disabled = false) => `<button class="button ${primary ? 'primary' : ''}" data-action="${action}" ${disabled ? 'disabled' : ''}>${label}</button>`;
// Adding a host skips naming: the daemon already exists.
const flow = () => state.mode === 'add-host' ? phases.filter(p => p !== 'identity') : state.mode === 'change-provider' ? ['model','installation','conversation'] : state.mode === 'update-daemon' ? ['installation'] : phases;
const hostHeadings = { host: 'Add OpenClaw to your daemon.', model: 'Connect OpenClaw to a model.', telegram: 'Create the OpenClaw bot.', installation: 'Starting OpenClaw.', conversation: 'First conversation on OpenClaw.' };
const updateHeadings = { installation: 'Updating your daemon.' };
const providerHeadings = { model: 'Choose another model provider.', installation: 'Switching the model connection.', conversation: 'Check your daemon’s reply.' };
const headings = { host: 'Install your daemon’s host.', identity: 'Name your daemon.', model: 'Connect your model.', review: 'Review your setup.', telegram: 'Connect Telegram.', installation: 'Installing your daemon.', conversation: 'First meeting.' };

function links() {
  const bot = state.links.map(url => /^https:\/\/web\.telegram\.org\/k\/#@([A-Za-z0-9_]{5,32})$/.exec(url)?.[1]).find(Boolean);
  const code = state.chatgptCode && !['authenticated','catalog'].includes(state.chatgptStage) ? '<p>Enter this code in the ChatGPT sign-in page:</p><p class="auth-code"><strong data-original>'+escape(state.chatgptCode)+'</strong></p>' : '';
  const buttons = state.links.map((url, i) => `<button class="text-button" data-link="${i}">${url.includes('auth.openai.com') ? 'Continue current ChatGPT connection' : url.includes('t.me/') ? 'Open bot link (Telegram app)' : url.includes('/k/#@') ? 'Open conversation in Telegram Web' : 'Open Telegram Web'} ↗</button>`).join('');
  return `${code}<div class="links">${buttons}</div>${bot ? `<div class="note"><b>Your bot: <span class="bot-name">@${escape(bot)}</span></b><p>If the link or Start button does not respond, search for this exact username in Telegram Web. Open the conversation, send <b>/start</b>, then wait for its reply. Keep this computer awake.</p></div>` : ''}`;
}
function promptBody() {
  const p = state.prompt;
  if (!p) return '';
  if (p.kind === 'choice') return `<form id="answer-form" data-prompt-id="${p.id}"><fieldset><legend class="field-label">${escape(p.title)}</legend>${p.options.map((option, i) => `<label class="choice"><input type="radio" name="answer" value="${i}" ${i === p.default ? 'checked' : ''}><b>${escape(option)}</b></label>`).join('')}</fieldset></form>`;
  return `<form id="answer-form" data-prompt-id="${p.id}"><label class="field-label" for="answer">${escape(p.title)}</label><input class="input" id="answer" type="${p.kind === 'secret' ? 'password' : 'text'}" maxlength="${p.maximum}" value="${escape(p.kind === 'secret' ? '' : p.previous)}" autocomplete="off" spellcheck="false" required>${p.kind === 'secret' ? '<p class="hint">Hidden while you type. Never included in the app’s activity messages.</p>' : ''}</form>`;
}
function details() {
  const messages = state.messages.filter(message => !state.guidance.includes(message));
  if (!messages.length) return '';
  return `<details class="activity-details"><summary>Setup details</summary><div>${messages.map(message => `<p>${guidanceText(message)}</p>`).join('')}</div></details>`;
}
function body() {
  if(state.page==='health'||state.page==='diagnostic')return reportBody();
  if(state.page==='discard')return discardBody();
  if(state.page==='removal')return removalBody();
  if(state.page==='daemon')return daemonBody();
  if(state.page==='backup')return backupBody();
  if(state.page==='restore')return restoreBody();
  if(state.page==='instances')return instanceBody();
  if (state.page === 'about') return aboutBody();
  if (state.page === 'settings') return settingsBody();
  const current = flow().indexOf(state.phase);
  return `<div class="steps" aria-label="${escape(labels[state.phase] || 'Setup')}">${flow().map((_, i) => `<span class="${i === current ? 'current' : i < current ? 'past' : ''}"></span>`).join('')}</div><div class="eyebrow">${escape(labels[state.phase] || 'Setup')}</div><h1>${escape(state.prompt?.title === 'Conversation language' ? 'Choose your conversation language.' : state.prompt?.title === 'Choose your model' ? 'Choose your model.' : state.prompt?.title === 'ChatGPT setup needs attention' ? 'Complete your ChatGPT setup.' : (state.mode === 'add-host' ? hostHeadings : state.mode === 'change-provider' ? providerHeadings : state.mode === 'update-daemon' ? updateHeadings : headings)[state.phase] || 'Setting things up.')}</h1>
    ${state.phase==='host'&&state.mode!=='add-host'?`<p class="hint">${escape(locale.t('setup.duration',language))}</p>`:''}
    <div id="activity" role="status" aria-live="polite">${activityBody()}</div>
    ${!state.prompt && state.result ? `<div class="note">${state.result.result === 'error' ? errorBody(state.result.message) : escape(state.result.message || 'Setup ended. See the details below for the verified result.')}</div>` : ''}
    ${state.guidance.length ? `<div class="guidance">${state.guidance.map(message => `<p>${guidanceText(message)}</p>`).join('')}${serviceStatus(...state.guidance)}</div>` : ''}
    ${links()}${promptBody()}${state.phase==='model'&&state.modelCatalog?modelCatalogBody(state.modelCatalog):''}${details()}<div class="links">${['add-host','change-provider','update-daemon'].includes(state.mode)?button("Export host diagnostic","host-diagnostic"):button("Diagnostic report","diagnostic")}</div>`;
}

let activityStartedAt = null, activityOutputAt = null;
const activitySeconds = () => Math.max(0, Math.floor((Date.now() - (activityStartedAt || Date.now())) / 1000));
// Reassure only once a step has really been quiet for a while.
const SILENCE_NOTICE_SECONDS = 30;
function elapsedActivity() {
  const seconds = activitySeconds();
  return `${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`;
}
function activityBody() {
  if (!state.active || state.prompt) return '';
  return `<div class="row busy"><span class="spinner" aria-hidden="true"></span><span>${escape(state.activity || 'Preparing the next step…')}</span></div>${serviceStatus(state.activity)}
    <p class="hint" aria-live="off">${escape(locale.t('setup.elapsed',language))}: <span data-activity-elapsed>${elapsedActivity()}</span></p>
    ${activityOutputAt?`<p class="hint">${escape(locale.t('setup.output',language))} <span data-original>${new Date(activityOutputAt).toLocaleTimeString(language)}</span></p>`:`<p class="hint" data-activity-silence ${activitySeconds()<SILENCE_NOTICE_SECONDS?'hidden':''}>${escape(locale.t('setup.silence',language))}</p>`}`;
}
setInterval(() => {const el=document.querySelector('[data-activity-elapsed]');if(el)el.textContent=elapsedActivity();const quiet=document.querySelector('[data-activity-silence]');if(quiet)quiet.hidden=activitySeconds()<SILENCE_NOTICE_SECONDS;},1000);
function rememberDraft() {
  const form=document.querySelector('#daemon-form');
  if(form && state.daemon?.token===form.dataset.review) {
    state.daemonDraft={...state.daemonDraft,...Object.fromEntries([...form.querySelectorAll('[data-daemon-field]')].map(el=>[el.dataset.daemonField,el.value]))};
  }
  if (!state.prompt) return;
  const input = document.querySelector('#answer');
  const choice = document.querySelector('[name=answer]:checked');
  if (document.querySelector('#answer-form')?.dataset.promptId !== String(state.prompt.id)) return;
  if (input || choice) draft = {id: state.prompt.id, value: input ? input.value : Number(choice.value)};
}
function languagePicker(id) {
  return `<label class="language-choice" for="${id}"><span class="sr-only">Language</span><select id="${id}" data-language ${languageSaving ? 'disabled' : ''}><option value="en" ${language === 'en' ? 'selected' : ''}>English</option><option value="fr" ${language === 'fr' ? 'selected' : ''}>Français</option></select></label>`;
}
async function changeTheme(value) {
  if (themeSaving) return;
  rememberDraft();
  themeSaving = true;
  try { theme = await window.companion.theme(value); }
  catch { state.error = 'Your appearance choice could not be saved. Please try again.'; }
  finally { themeSaving = false; render(); document.querySelector(`[data-theme-choice][value="${theme}"]`)?.focus(); }
}
const updateErrors = ['update.access','update.unreachable','update.invalid','update.verify','update.failed','update.busy'];
const updateError = () => updateErrors.includes(update.error) ? `<p class="error">${escape(locale.t(update.error,language))}</p>${codeStatus(update.error)}` : '';
// What the app says about its own update, wherever it is shown.
function updateNote() {
  const hide = '<div class="links"><button class="text-button" data-update-dismiss>Hide</button></div>';
  if(update.notice==='installed')return `<div class="note update-note" role="status"><p><strong>The app was updated.</strong></p>${hide}</div>`;
  if(update.notice==='failed')return `<div class="note update-note" role="status"><p><strong>The update could not be installed. The previous version was kept.</strong></p>${hide}</div>`;
  if(update.state==='available')return `<div class="note update-note"><p><strong>A new version of the app is available.</strong></p><p>The app closes, installs it and reopens by itself. Your daemons keep running.</p>${updateError()}<div class="links"><button class="button primary" data-update-install ${updateBusy||state.active?'disabled':''}>Install and restart</button></div></div>`;
  if(update.state==='downloading')return `<div class="note update-note" role="status"><div class="row busy"><span class="spinner" aria-hidden="true"></span><span data-update-progress>${escape(locale.t('update.downloading',language,{percent:update.progress??0}))}</span></div></div>`;
  if(update.state==='installing')return `<div class="note update-note" role="status"><div class="row busy"><span class="spinner" aria-hidden="true"></span><span>Installing the update. The app reopens by itself.</span></div></div>`;
  return '';
}
function updateSection() {
  const version = `<dl class="about-meta settings-meta"><div><dt>App version</dt><dd data-original>${escape(appInfo.version || '—')}${appInfo.revision ? ` · ${escape(appInfo.revision)}` : ''}</dd></div>${update.enabled?`<div><dt>Update channel</dt><dd>${update.channel==='candidate'?'Candidate versions':'Stable versions'}</dd></div>`:''}</dl>`;
  if(!update.enabled)return `<div class="about-section"><h2>Updates</h2><p>Updates are not available for this copy of the app.</p>${version}<div class="links"><button class="text-button" data-update-export>Export the update diagnostic</button></div></div>`;
  const line = update.state==='checking' ? 'Checking for updates…' : update.state==='current' ? 'The app is up to date.' : '';
  return `<div class="about-section" id="settings-updates"><h2>Updates</h2>${version}${line?`<p>${line}</p>`:''}${updateNote()}${update.state==='failed'?updateError():''}<div class="links"><button class="text-button" data-update-check ${['checking','downloading','installing'].includes(update.state)||updateBusy?'disabled':''}>Check for updates</button><button class="text-button" data-update-export>Export the update diagnostic</button></div></div>`;
}
// Elsewhere, only a short line points to Settings; the outcome of an update is shown where the app reopens.
function updateReminder() {
  if(update.notice)return updateNote();
  if(update.state!=='available')return '';
  return '<div class="note update-note update-reminder"><strong>A new version of the app is available.</strong><button class="text-button" data-action="settings-updates">Open settings</button></div>';
}
function settingsBody() {
  const choice=(value,label)=>`<label><input type="radio" name="theme-choice" value="${value}" data-theme-choice ${theme===value?'checked':''} ${themeSaving?'disabled':''}>${label}</label>`;
  return `<div class="eyebrow">Settings</div><h1>App settings.</h1><p class="intro">Language, appearance and updates of this app.</p>
    <div class="about-section"><h2>Language</h2>${languagePicker('settings-language')}</div>
    <div class="about-section"><h2>Appearance</h2><fieldset class="segment"><legend class="sr-only">Appearance</legend>${choice('light','Light')}${choice('dark','Dark')}${choice('system','System')}</fieldset><p class="hint">System follows your computer’s appearance. Light and Dark apply only to this app.</p></div>
    ${backgroundSection()}${googleSection()}${updateSection()}`;
}
function backgroundSection() {
  const b=backgroundMode,where=b.platform==='win32'?'Keep the app running in the notification area when its window is closed':'Keep the app running in the menu bar when its window is closed';
  return `<div class="about-section" id="settings-background"><h2>Background</h2>
    <label class="check"><input type="checkbox" data-background="enabled" ${b.enabled?'checked':''} ${b.busy?'disabled':''}><span>${where}</span></label>
    <label class="check"><input type="checkbox" data-background="login" ${b.openAtLogin?'checked':''} ${b.busy||!b.enabled?'disabled':''}><span>Open at login</span></label>
    <p class="hint">Daily backups need the app running. Its icon reopens the window or quits the app.</p></div>`;
}
async function changeBackground(kind,checked) {
  if(backgroundMode.busy)return;
  const next={enabled:kind==='enabled'?checked:backgroundMode.enabled,openAtLogin:kind==='login'?checked:kind==='enabled'&&checked?true:backgroundMode.openAtLogin};
  backgroundMode.busy=true;render();
  try {const saved=await window.companion.background(next.enabled,next.openAtLogin);backgroundMode={...backgroundMode,enabled:saved.background,openAtLogin:saved.openAtLogin};}
  catch(error){showError(error);}
  finally {backgroundMode.busy=false;render();}
}
function googleSection() {
  const busy=google.busy?'<p role="status" class="row"><span class="spinner" aria-hidden="true"></span>Waiting for Google in your browser…</p>':'';
  if(!google.configured)return '<div class="about-section" id="settings-google"><h2>Google Drive</h2><p>Google Drive backups are not available in this build.</p></div>';
  return `<div class="about-section" id="settings-google"><h2>Google Drive</h2>${google.connected
    ?`<p>Connected. Backups go to the Daemons / Backups folder of your Google Drive.</p><button class="text-button" data-google="disconnect" ${google.busy?'disabled':''}>Disconnect Google Drive</button><p class="hint">Disconnecting stops daily backups. Backups already in your Drive stay there.</p>`
    :`<p>Connect your Google account to back up memories to your Google Drive. Daemons only sees the files it creates there.</p><button class="button" data-google="connect" ${google.busy?'disabled':''}>Connect Google Drive</button>`}${busy}</div>`;
}
async function googleAction(kind) {
  if(google.busy)return;
  google.busy=true;state.error='';render();
  try {if(kind==='connect')await window.companion.connectGoogle();else await window.companion.disconnectGoogle();}
  catch(error){showError(error);}
  finally {google.busy=false;await refreshGoogle();render();}
}
function settingsButton() {
  const selected=state.page==='settings', pending=update.state==='available'||Boolean(update.notice);
  return `<button class="settings-button ${selected?'active':''}" data-action="settings" ${selected?'aria-current="page"':''}><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 0 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 0 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 0 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 0 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z"></path></svg><span>Settings</span>${pending?'<span class="settings-dot" aria-hidden="true"></span>':''}</button>`;
}
async function installUpdate() {
  if(updateBusy)return;
  updateBusy=true;render();
  try {update=await window.companion.installUpdate();}
  catch {update={...update,error:'update.failed'};}
  finally {updateBusy=false;render();}
}
function aboutBody() {
  return `<div class="eyebrow">About Daemons</div><h1>A personal cognitive system.</h1><p class="intro">Daemons is an open-source cognitive system for a personal AI assistant: your daemon. It leads your conversations with its own identity, memory and continuity, which develop through your exchanges. A host agent gives it model access, the harness and communication channels.</p>
    <div class="about-section"><h2>Continuity across conversations</h2><p>Your daemon keeps useful memories, connects events and follows changes in your projects, preferences and circumstances. It retrieves relevant context to respond with greater depth and attention, without starting from scratch in every conversation.</p></div>
    <div class="about-section"><h2>Your memory, also in Obsidian</h2><p>Your memory belongs to you. It stays on your computer as readable, editable Markdown files. Open its folder as an Obsidian vault to read, correct and organize your notes, explore their links and search for information.</p><p>Memories are organized in five folders: Projects, Areas, Resources, Garden and Archives. Obsidian works directly with the same notes as the daemon, with no export or separate copy to synchronize. It is optional: you can also use another Markdown editor.</p><button class="text-button" data-project-link="obsidian">Download Obsidian ↗</button></div>
    <div class="about-section"><h2>The daemon and its host</h2><p>Think of the daemon as the mind and its host agent as the body. The daemon carries its identity, memories and the continuity of your shared experience. Hermes Agent or OpenClaw provides its tools, model access and communication channels. You can change hosts while retaining that history and experience.</p></div>
    <div class="about-section"><h2>This app</h2><p>This app installs your daemon on Hermes Agent or OpenClaw and manages your local installations. After setup, you can close it and continue your conversations on Telegram.</p></div>
    <dl class="about-meta"><div><dt>Created by</dt><dd data-original>${escape(appInfo.author || '')}</dd></div><div><dt>App version</dt><dd data-original>${escape(appInfo.version || '—')}${appInfo.revision ? ` · ${escape(appInfo.revision)}` : ''}</dd></div></dl>
    <div class="links"><button class="text-button" data-project-link="project">Project on GitHub ↗</button><button class="text-button" data-project-link="contact">Contact via GitHub ↗</button></div><p class="hint">GitHub opens in your browser. Access to the repository is required.</p>`;
}
async function changeLanguage(value) {
  rememberDraft();
  languageSaving = true;
  try { language = await window.companion.language(value); }
  catch { state.error = 'Your language choice could not be saved. Please try again.'; }
  finally { languageSaving = false; render(); document.querySelector('[data-language]')?.focus(); }
}
function render() {
  if (!locale) return;
  rememberDraft();
  document.querySelectorAll('[data-instance-card]').forEach(el=>{if(el.open)openHostDetails.add(el.dataset.instanceCard);else openHostDetails.delete(el.dataset.instanceCard);});
  const hostScroll=state.page==='instances'?document.querySelector('.content')?.scrollTop||0:0;
  if(state.page==='removal'&&document.querySelector('#removal-name')&&!removalState.busy)removalState.name=document.querySelector('#removal-name').value;
  if(state.page==='backup'&&document.querySelector('#backup-password')&&!backupState.busy){backupState.password=document.querySelector('#backup-password').value;backupState.confirm=document.querySelector('#backup-confirm').value;}
  if(state.page==='restore'&&document.querySelector('#restore-password')&&!restoreState.busy)restoreState.password=document.querySelector('#restore-password').value;
  const daemonScroll = state.page==='daemon' && state.daemon && document.querySelector('#daemon-form') ? document.querySelector('.content').scrollTop : 0;
  // Settings and About re-render in place (an update starting, Drive connecting): keep the reader where they were.
  const pageScroll=['settings','about'].includes(state.page)&&renderedPage===state.page?document.querySelector('.content')?.scrollTop||0:0;
  const p = state.prompt;
  const runningSetup = state.active && state.operation === 'setup';
  let controls;
  if(['health','diagnostic'].includes(state.page))controls=button('Back','report-back',false,reportState.busy)+button('Refresh','report-refresh',false,reportState.busy)+button('Export diagnostic','report-export',true,reportState.busy||!(state.page==='health'?reportState.health:reportState.diagnostic));
  else if(state.page==='discard')controls=button(discardState.plan?.done?'Back to my daemons':'Cancel','discard-back',false,discardState.busy)+(discardState.plan&&!discardState.plan.done?button('Delete unfinished installation','discard-confirm',false,discardState.busy):'');
  else if(state.page==='removal')controls=button(removalState.plan?.done?'Back to my daemons':'Cancel','removal-back',false,removalState.busy)+(removalState.plan&&!removalState.plan.done?button(removalState.plan.recovery?'Recover removal':removalState.plan.action==='detach'?'Remove daemon':removalState.deleteMemory?'Uninstall and delete memory':'Confirm uninstall','removal-confirm',false,removalState.busy||removalState.name!==removalState.plan.confirmationText):'');
  else if(state.page==='backup')controls=button(backupState.result?'Back to daemon settings':'Cancel','backup-back',false,backupState.busy)+(backupState.result?'':button(backupState.mode==='automatic'?'Turn on daily backup':'Create backup','backup-confirm',true,backupState.busy));
  else if(state.page==='restore')controls=button('Cancel','restore-back',false,restoreState.busy)+(restoreState.step==='source'?'':button('Install this daemon','restore-confirm',true,restoreState.busy));
  else if(state.page==='daemon')controls=button('Back to my daemons','daemon-back',false,state.daemonBusy)+(state.daemon?.canEditName?button('Save changes','daemon-save',true,state.daemonBusy||!daemonDirty()):'');
  else if(state.page==='instances')controls=button('Refresh list','instance-refresh',false,state.instanceBusy)+button('Add an existing installation','instance-add',false,state.instanceBusy||state.active)+button('Install a daemon from a backup','restore-choose',false,state.instanceBusy||state.active)+button(runningSetup?'Return to setup':'Install a daemon',runningSetup?'setup':'new-instance',true,state.instanceBusy||(state.active&&!runningSetup));
  else if (state.page === 'setup') controls = p ? `${button('Back', 'back')}${button('Continue', 'submit', true, p.kind !== 'choice' && !p.previous)}${button('Stop setup', 'cancel')}` : state.active ? button('Stop setup', 'cancel') : button('My daemons', 'instances', true);
  else controls = button(runningSetup ? 'Return to setup' : 'My daemons', runningSetup ? 'setup' : 'instances', true);
  document.querySelector('#app').innerHTML = `<div class="shell"><aside><div class="brand"><img class="brand-logo" src="assets/daemons-dervaux.svg" alt="Daemons" width="128" height="32"></div><nav aria-label="Application">${Object.entries(navigation).map(([name,label]) => { const selected = state.page === name || (name === 'instances' && ['daemon','removal','discard','health','diagnostic','host-add','backup','restore'].includes(state.page)); return `<button class="nav ${selected ? 'active' : ''}" data-action="${name}" ${selected ? 'aria-current="page"' : ''}>${label}</button>${name === 'instances' && (state.page === 'setup' || (state.active && state.operation === 'setup')) ? `<button class="nav nav-context ${state.page === 'setup' ? 'active' : ''}" data-action="setup" ${state.page === 'setup' ? 'aria-current="page"' : ''}>${state.active ? 'Setup in progress' : 'Setup summary'}</button>` : ''}`; }).join('')}</nav>${settingsButton()}</aside><main>${topbar()}<div class="content">${body()}<div class="error" role="alert">${errorBody(state.page==='daemon'?'':state.error)}</div></div><footer class="footer">${state.page==='daemon'?'<div id="daemon-feedback" class="daemon-feedback"><div id="daemon-status" role="status" aria-live="polite" aria-atomic="true"></div><div id="daemon-error" class="error" role="alert"></div></div>':`<div class="footer-note">${state.page === 'setup' && state.active ? 'Getting started · changes only after your choices' : ''}</div>`}${controls}</footer></main></div>`;
  document.querySelectorAll('[data-action]').forEach(element => element.addEventListener('click', () => action(element.dataset.action)));
  document.querySelector('[data-started-toggle]')?.addEventListener('click',()=>{started.open=!started.open;render();if(started.open)refreshStarted();});
  document.querySelectorAll('[data-started-step]').forEach(el=>el.addEventListener('click',()=>startedStep(el.dataset.startedStep)));
  document.querySelector('#backup-form')?.addEventListener('submit', event => { event.preventDefault(); action('backup-confirm'); });
  document.querySelector('#restore-form')?.addEventListener('submit', event => { event.preventDefault(); action('restore-confirm'); });
  document.querySelectorAll('[data-removal]').forEach(element=>element.addEventListener('click',()=>openRemoval(element.dataset.instance,element.dataset.removal)));
  document.querySelector('[data-removal-memory]')?.addEventListener('change',event=>{removalState.deleteMemory=event.target.checked===true;render();});
  document.querySelector('#removal-name')?.addEventListener('input',event=>{removalState.name=event.target.value;document.querySelector('[data-action=removal-confirm]').disabled=removalState.busy||removalState.name!==removalState.plan.confirmationText;});
  document.querySelectorAll('[data-setup-resume]').forEach(el=>el.addEventListener('click',()=>resumeSetup(el.dataset.setupResume)));
  document.querySelectorAll('[data-setup-discard]').forEach(el=>el.addEventListener('click',()=>openDiscard(el.dataset.setupDiscard)));
  document.querySelectorAll('[data-setup-report]').forEach(el=>el.addEventListener('click',()=>openReport('diagnostic',el.dataset.setupReport)));
  document.querySelectorAll('[data-health-export]').forEach(el=>el.addEventListener('click',()=>reportAction('report-export',el.dataset.healthExport||null)));
  document.querySelectorAll('[data-health-detail]').forEach(el=>el.addEventListener('toggle',()=>{if(el.open)reportState.expanded.add(el.dataset.healthDetail);else reportState.expanded.delete(el.dataset.healthDetail);}));
  document.querySelector('[data-health-start]')?.addEventListener('click',async()=>{reportState.busy=true;state.error='';render();try{await window.companion.instanceAction(reportState.id,'start');await loadReport();}catch(e){state.error=e.message;}finally{reportState.busy=false;render();}});
  document.querySelector('[data-health-restart]')?.addEventListener('click',async()=>{reportState.busy=true;state.error='';render();try{await restartWithStatus(reportState.id);await loadReport();scheduleChannels();}catch(e){state.error=e.message;}finally{reportState.busy=false;render();}});
  document.querySelectorAll('[data-host-interface]').forEach(el=>el.addEventListener('click',async()=>{
    if(hostInterfaceState.busy)return;
    const id=el.dataset.hostInterface;Object.assign(hostInterfaceState,{id,busy:true,message:''});render();
    try{await window.companion.openHostInterface(id);hostInterfaceState.message='hostInterface.opened';}
    catch(e){const key=String(e.message||'').replace(/^Error invoking remote method '[^']+': Error: /,'');hostInterfaceState.message=['hostInterface.busy','hostInterface.stopped','hostInterface.unavailable','hostInterface.external','hostInterface.older','hostInterface.disabled','hostInterface.assets','hostInterface.failed','hostInterface.timeout'].includes(key)?key:'hostInterface.unavailable';}
    finally{hostInterfaceState.busy=false;render();}
  }));
  document.querySelector('[data-obsidian-open]')?.addEventListener('click',async()=>{
    if(obsidianBusy)return;
    const id=state.daemon.id;obsidianBusy=true;obsidianMessage='';render();
    try {const result=await window.companion.openObsidian(id);if(state.daemon?.id===id)obsidianMessage=result.added?'obsidian.added':'obsidian.opened';}
    catch(e){if(state.daemon?.id===id){const key=String(e.message||'').replace(/^Error invoking remote method '[^']+': Error: /,'');obsidianMessage=['obsidian.unavailable','obsidian.openFailed','obsidian.closeFirst','obsidian.processUnknown','obsidian.configUnsafe','obsidian.configChanged','obsidian.registrationBusy','obsidian.memoryUnavailable','obsidian.registrationFailed'].includes(key)?key:'obsidian.registrationFailed';}}
    finally {if(state.daemon?.id===id)obsidianInfo=await window.companion.obsidianStatus(id).catch(()=>obsidianInfo);obsidianBusy=false;render();}
  });
  bindProvider();
  document.querySelector('[data-obsidian-refresh]')?.addEventListener('click',async()=>{try{obsidianInfo=await window.companion.obsidianStatus(state.daemon.id);obsidianMessage='';render();}catch(e){showError(e);}});
  document.querySelectorAll('[data-health]').forEach(element=>element.addEventListener('click',()=>openReport('health',element.dataset.health)));
  document.querySelectorAll('[data-host-add]').forEach(element=>element.addEventListener('click',()=>startHostAddition(element.dataset.hostAdd)));
  document.querySelectorAll('[data-daemon-manage]').forEach(element=>element.addEventListener('click',()=>openDaemon(element.dataset.daemonManage)));
  document.querySelector('#daemon-pronouns')?.addEventListener('change',()=>{daemonEdited();render();document.querySelector(state.daemonPronounChoice==='custom'?'#daemon-pronouns-custom':'#daemon-pronouns')?.focus();});
  document.querySelectorAll('[data-daemon-field],[data-daemon-pronouns-custom]').forEach(element=>element.addEventListener('input',daemonEdited));
  document.querySelector('#daemon-form')?.addEventListener('submit',event=>{event.preventDefault();saveDaemon();});
  document.querySelectorAll('[data-instance-action]').forEach(element=>element.addEventListener('click',()=>instanceAction(element.dataset.instance,element.dataset.instanceAction)));
  document.querySelectorAll('[data-daemon-restart]').forEach(element=>element.addEventListener('click',()=>restartDaemon(element.dataset.daemonRestart)));
  document.querySelectorAll('[data-instance-open]').forEach(element=>element.addEventListener('click',()=>window.companion.instanceOpen(element.dataset.instance,element.dataset.instanceOpen).catch(showError)));
  document.querySelectorAll('[data-link]').forEach(element => element.addEventListener('click', () => window.companion.openLink(state.links[Number(element.dataset.link)]).catch(showError)));
  document.querySelectorAll('[data-project-link]').forEach(element => element.addEventListener('click', () => window.companion.projectLink(element.dataset.projectLink).catch(showError)));
  document.querySelectorAll('[data-update-install]').forEach(element => element.addEventListener('click', installUpdate));
  document.querySelectorAll('[data-update-dismiss]').forEach(element => element.addEventListener('click', async () => {update=await window.companion.dismissUpdate().catch(()=>update);render();}));
  document.querySelectorAll('[data-google]').forEach(element => element.addEventListener('click', () => googleAction(element.dataset.google)));
  document.querySelectorAll('[data-restore-drive]').forEach(element => element.addEventListener('click', () => restoreFromDrive(element.dataset.restoreDrive)));
  document.querySelectorAll('[data-restore-kind]').forEach(element => element.addEventListener('change', () => {if(document.querySelector('#restore-password'))restoreState.password=document.querySelector('#restore-password').value;restoreState.move=element.value==='move';render();}));
  document.querySelectorAll('[data-backup-destination]').forEach(element => element.addEventListener('change', () => {backupState.destination=element.value;render();}));
  document.querySelectorAll('[data-update-check]').forEach(element => element.addEventListener('click', async () => {try {update=await window.companion.checkUpdate();} catch {update={...update,state:'failed',error:'update.failed'};} render();}));
  document.documentElement.lang = language;
  locale.localizeElement(document.body, language);
  document.querySelectorAll('[data-language]').forEach(element => element.addEventListener('change', () => changeLanguage(element.value)));
  document.querySelectorAll('[data-background]').forEach(element => element.addEventListener('change', () => changeBackground(element.dataset.background, element.checked)));
  document.querySelectorAll('[data-theme-choice]').forEach(element => element.addEventListener('change', () => changeTheme(element.value)));
  document.querySelectorAll('[data-update-export]').forEach(element => element.addEventListener('click', () => window.companion.exportDiagnostic('update', null).catch(showError)));
  if(state.page==='instances')document.querySelector('.content').scrollTop=hostScroll;
  if(state.page==='daemon') { updateDaemonFeedback(); document.querySelector('.content').scrollTop=daemonScroll; }
  if(pageScroll)document.querySelector('.content').scrollTop=pageScroll;
  renderedPage=state.page;
  const input = document.querySelector('#answer');
  if (p && draft && draft.id === p.id) {
    if (input) input.value = draft.value;
    else { const radio = document.querySelector(`[name=answer][value="${draft.value}"]`); if (radio) radio.checked = true; }
  }
  if (input) document.querySelector('[data-action=submit]').disabled = !input.value.trim();
  input?.addEventListener('input', () => { document.querySelector('[data-action=submit]').disabled = !input.value.trim(); });
  document.querySelector('#answer-form')?.addEventListener('submit', event => { event.preventDefault(); action('submit'); });
  if (state.page === 'setup') input?.focus();
}
function errorBody(value) {
  if (!value) return '';
  const clean = value.replace(/^Error invoking remote method '[^']+': Error: /, '');
  if(clean.startsWith('attachment.')){try{return escape(locale.t(clean,language));}catch{return escape(locale.t('attachment.unconfirmed',language));}}
  if(clean.startsWith('change.')){const [code]=clean.split('|');try{return escape(locale.t(code,language));}catch{return escape(locale.t('change.unavailable',language));}}
  if(['obsidian.unavailable','obsidian.openFailed'].includes(clean))return escape(locale.t(clean,language));
  if(/^(backup|archive|restore|google|drive)\./.test(clean)){
    // A code may carry a technical detail after '|': the error class and numbers, never paths.
    const [code,detail='']=clean.split('|');let text,known=true;
    try{text=locale.t(code,language);}catch{known=false;text=locale.t(code.startsWith('archive.')?'archive.invalid':'backup.unavailable',language);}
    const extra=[known?'':code,detail].filter(Boolean).join(' · ');
    return escape(text)+(extra?`<span class="error-detail">${escape(locale.t('error.detail',language))} <span data-original>${escape(extra)}</span></span>`:'')+codeStatus(code);
  }
  if (language === 'fr' && !locale.identify(clean)) return `<span>${escape(locale.t('error.detailsNotice', language))}</span><details><summary>${escape(locale.t('error.technical', language))}</summary><pre data-original>${escape(value)}</pre></details>`;
  return escape(clean)+serviceStatus(clean);
}
function showError(error) { state.error = error.message || 'The action could not complete.'; render(); }
async function action(name) {
  if(removalState.busy||discardState.busy||reportState.busy)return;
  if(name==='host-diagnostic'){try{await window.companion.exportDiagnostic('host',hostAdditionState.id);}catch(error){showError(error);render();}return;}
  if(name.startsWith('discard-')){await discardAction(name);return;}
  if(name==='diagnostic'){await openReport('diagnostic');return;}
  if(name.startsWith('report-')){await reportAction(name);return;}
  if(name.startsWith('removal-')){await removalAction(name);return;}
  if(name==='daemon-save'){await saveDaemon();return;}
  if(name==='daemon-backup'||name==='daemon-backup-auto'){if(state.daemonBusy||!discardDaemonDraft())return;Object.assign(backupState,{id:state.daemon.id,name:state.daemon.values?.name||'',busy:false,result:null,password:'',confirm:'',mode:name==='daemon-backup-auto'?'automatic':'once',destination:'file'});state.page='backup';state.error='';render();return;}
  if(name==='daemon-host-change'){
    if(state.daemonBusy||!discardDaemonDraft())return;
    state.daemonBusy=true;state.error='';render();
    try {
      await window.companion.startHostChange(state.daemon.id);
      state.daemon=null;state.daemonDraft=null;
      draft=null;state.page='setup';state.mode='setup';state.operation='setup';state.savedSetup='saved';state.overview=null;state.setupStarted=true;state.active=true;state.result=null;state.messages=[];state.guidance=[];state.links=[];state.prompt=null;state.activity='Preparing your private setup…';
    } catch(error){state.error=error.message;}
    finally {state.daemonBusy=false;render();}
    return;
  }
  if(name==='daemon-backup-off'){state.daemonBusy=true;render();try{daemonBackup=(await window.companion.automaticBackup(state.daemon.id,false)).automatic;}catch(error){showError(error);}finally{state.daemonBusy=false;render();}return;}
  if(name.startsWith('backup-')){if(!backupState.busy)await backupAction(name);return;}
  if(name.startsWith('restore-')){if(!restoreState.busy)await restoreAction(name);return;}
  if(name==='daemon-reload'){if(discardDaemonDraft())await openDaemon(state.daemon.id);return;}
  if(name==='daemon-copy'){try{await window.companion.copyMemoryPath(state.daemon.id);state.daemonMessage='Memory path copied.';render();}catch(error){showError(error);}return;}
  if(name==='daemon-back')name='instances';
  if(state.page==='daemon' && ['welcome','instances','about','settings','settings-updates','setup','start','new-instance'].includes(name)) {
    if(state.daemonBusy || !discardDaemonDraft())return;
    state.daemon=null;state.daemonDraft=null;state.error='';
  }
  if(name==='instances'){rememberDraft();state.page='instances';await refreshInstances();return;}
  if(name==='instance-refresh'||name==='instance-add'){await refreshInstances(name==='instance-add');return;}
  state.error = '';
  if (name === 'memory') { await window.companion.openMemory().catch(showError); return; }
  if (name === 'status') { state.operation='status'; state.active = true; state.activity = 'Checking your private installation…'; render(); try { await window.companion.status(); } catch(error) { state.active = false; showError(error); } return; }
  if (name === 'about' || name === 'settings') { state.page = name; render(); if(name==='settings'){await refreshGoogle();render();} return; }
  if (name === 'settings-updates') { state.page = 'settings'; render(); document.querySelector('#settings-updates')?.scrollIntoView({block:'start'}); return; }
  if (name === 'setup' && !state.active) { state.page = 'instances'; await refreshInstances(); return; }
  if (name === 'setup' || name === 'start' || name === 'new-instance') {
    state.page = 'setup';
    if (state.active) { render(); return; }
    state.mode='setup'; state.operation='setup'; state.savedSetup='saved'; state.overview=null; state.setupStarted=true; state.active = true; state.result = null; state.messages = []; state.guidance = []; state.links = []; state.prompt = null; state.activity = 'Preparing your private setup…';
    render();
    try {
      if(name==='new-instance') { draft=null; state.overview=null; await window.companion.createInstance(); }
      else await window.companion.start(state.resumeId||null);
      state.resumeId=null;
    } catch (error) { state.active = false; showError(error); }
    return;
  }
  if (name === 'cancel') {
    // Confirmation stays inside the window; no automatic answer or secret resend.
    if (!window.confirm(locale.t('cancel.confirm', language))) return;
    draft = null; state.prompt = null; state.activity = 'Stopping safely. Please wait…'; render();
    await window.companion.cancel().catch(showError); return;
  }
  if (!state.prompt) return;
  const prompt = state.prompt;
  const value = name === 'back' ? null : prompt.kind === 'choice' ? Number(document.querySelector('[name=answer]:checked').value) : document.querySelector('#answer').value;
  if (name !== 'back' && prompt.kind !== 'choice' && !value.trim()) return;
  const answer = { id: prompt.id, action: name === 'back' ? 'back' : 'submit', value };
  // Remove the password field synchronously, before crossing IPC.
  draft = null; state.prompt = null; render();
  try { await window.companion.answer(answer); } catch (error) { state.prompt = prompt.kind === 'secret' ? {...prompt, previous: ''} : prompt; showError(error); }
}
window.companion.onEvent(event => {
  if(event.type==='diagnostic')return;
  if(event.type==='model-catalog'){state.modelCatalog=event.catalog;render();return;}
  if(event.type==='chatgpt-stage'){state.chatgptStage=event.stage;if(['authenticated','catalog'].includes(event.stage)){state.chatgptCode='';state.links=state.links.filter(url=>!url.includes('auth.openai.com'));}render();return;}
  if(event.type==='sign-in-ended'){state.chatgptCode='';state.links=state.links.filter(url=>!url.includes('auth.openai.com'));render();return;}
  if(event.type==='chatgpt-code'){state.chatgptCode=event.code;render();return;}
  if (event.type === 'navigate-about') { action('about'); return; }
  if (event.type === 'update') {
    // Progress changes only its own line, so a form being filled is not redrawn.
    const progress = update.state==='downloading' && event.status.state==='downloading' && document.querySelector('[data-update-progress]');
    update = event.status;
    if (progress) { progress.textContent = locale.t('update.downloading',language,{percent:update.progress??0}); return; }
    render(); return;
  }
  if (event.type === 'activity') {
    if(event.active && activityStartedAt===null)activityStartedAt=Date.now();
    if(!event.active){activityStartedAt=null;activityOutputAt=null;}
    if(event.detail)activityOutputAt=Date.now();
    if(!event.detail)state.activity = event.active ? event.message : '';
    const element = document.querySelector('#activity');
    if (element) { element.innerHTML = activityBody(); if (locale) locale.localizeElement(element, language); }
    return;
  }
  if (event.type === 'overview') { state.overview = event; state.savedSetup = event.installed ? 'installed' : 'saved'; }
  if (event.type === 'connection-start') {state.guidance=[];state.error='';state.links = [];state.chatgptCode='';state.chatgptStage='starting';state.modelCatalog=null;}
  if (event.type === 'phase') { state.chatgptCode=''; state.guidance = []; if (state.phase !== event.name) state.links = []; state.phase = event.name; }
  // A ChatGPT sign-in link serves only while that sign-in runs; the next question means it is over.
  if (event.type === 'prompt') { state.links=state.links.filter(url=>!url.includes('auth.openai.com')); if(event.title==='3. Model access to prepare'){state.modelCatalog=null;state.chatgptStage=null;state.chatgptCode='';} activityStartedAt=null; draft = null; state.prompt = event; state.active = true; state.guidance = event.guidance || []; }
  if (event.type === 'message') { state.messages.push(event.message); state.messages = state.messages.slice(-100); state.guidance = [event.message]; }
  if (event.type === 'link') {
    if (!state.links.includes(event.url)) state.links.push(event.url);
    if (event.open === true) window.companion.openLink(event.url).catch(showError);
  }
  if (event.type === 'input-rejected') state.error = event.message;
  if (event.type === 'finished') {
    draft = null; state.result = event; state.prompt = null;
    if(event.result === 'installed' && !['change-provider','update-daemon'].includes(state.mode))state.savedSetup = 'installed';
  }
  if (event.type === 'idle') {state.active = false;if(state.page==='instances'){refreshInstances();return;}}
  render();
});
document.body.classList.toggle('mac', /Mac/.test(navigator.platform));
document.body.classList.toggle('windows', /Win/.test(navigator.platform));
window.companion.preferences().then(value => {
  locale = createLocalizer(value.catalog); language = value.language === 'fr' ? 'fr' : 'en';
  state.savedSetup = ['none','saved','installed','unreadable'].includes(value.setup) ? value.setup : 'none';
  theme = ['light','dark','system'].includes(value.theme) ? value.theme : 'light'; appInfo = value.about || {}; if (value.update) update = value.update;
  backgroundMode={enabled:value.background===true,openAtLogin:value.openAtLogin===true,platform:value.platform||'darwin',busy:false}; refreshInstances();
}).catch(() => { document.querySelector('#app').textContent = 'The app could not load its display settings. Please reopen it.'; });

function hostName(r) {return r.host==='hermes'?'Hermes Agent':'OpenClaw';}
function hostControls(r) {
  const action=(r,name,label)=>`<button class="button" data-instance="${r.id}" data-instance-action="${name}" ${state.instanceBusy||state.active||(['start','stop','restart'].includes(name)&&r.state==='unknown')?'disabled':''}>${label}</button>`;
  const open=(r,kind,label)=>`<button class="text-button" data-instance="${r.id}" data-instance-open="${kind}"><span>${label}</span> ↗</button>`;
  return `<details class="instance-details host-details" data-instance-card="${r.id}" ${openHostDetails.has(r.id)?'open':''}><summary><span>Host details</span>: <span data-original>${hostName(r)}</span></summary><div class="host-detail-body"><p class="small" data-original>${escape(r.profile)}</p>
    <p class="host-status"><strong><span>Host status</span>: <span>${{running:'Running',stopped:'Stopped',unknown:'Status unavailable'}[r.state]}</span></strong></p>
    ${r.daemon?`<p class="hint">${r.daemon.state==='configured'?'Selected in the host configuration. Live loading is not verified here.':'Plugin files detected. Live loading is not verified here.'}</p>`:''}
    ${r.problem?'<p class="error">The configuration could not be read. No service action is available.</p>':''}
    ${r.serviceProblem?`<p class="small">${{ambiguous:'Several services match this profile. Gateway controls are unavailable.',unavailable:'No supported gateway service was found for this profile.',changed:'The gateway definition changed. Connect this instance again to review it.'}[r.serviceProblem]}</p>`:''}
    <p class="hint">${escape(locale.t('hostInterface.help',language))}</p><button class="button" data-host-interface="${r.id}" ${hostInterfaceState.busy||state.active?'disabled':''} aria-busy="${hostInterfaceState.busy&&hostInterfaceState.id===r.id}">${hostInterfaceState.busy&&hostInterfaceState.id===r.id?'<span class="spinner button-spinner" aria-hidden="true"></span>':''}${escape(locale.t(hostInterfaceState.busy&&hostInterfaceState.id===r.id?'hostInterface.opening':'hostInterface.open',language))}</button>
    ${hostInterfaceState.id===r.id?`<p class="host-interface-status" role="status" aria-live="polite">${escape(locale.t(hostInterfaceState.busy?'hostInterface.wait':hostInterfaceState.message||'hostInterface.open',language))}</p>`:''}
    <div class="instance-actions">${open(r,'profile' ,'Open installation folder')}${r.managed?`${action(r,'start','Start')}${action(r,'stop','Stop')}${action(r,'restart','Restart')}`:r.canConnect?action(r,'connect','Enable host controls'):''}${r.registered?action(r,'forget','Forget connection'):''}</div>
    <details class="instance-details removal-options"><summary>Uninstall…</summary>${r.removalSupported&&r.host==='openclaw'?`<p class="small">Your memory is kept.</p>
    <div class="removal-choice"><button class="text-button" data-instance="${r.id}" data-removal="uninstall" ${state.active?'disabled':''}>Uninstall OpenClaw and the daemon…</button><p>Remove both programs and stop this installation from replying on Telegram.</p></div>`:r.removalSupported?`<p class="small">Choose what to remove from this installation. Your memory is kept in both cases.</p>
    <div class="removal-choice"><button class="text-button" data-instance="${r.id}" data-removal="uninstall" ${state.active?'disabled':''}>${r.daemon?'Uninstall Hermes and the daemon…':'Uninstall Hermes…'}</button><p>${r.daemon?'Remove both programs and stop this installation from replying on Telegram.':'Remove Hermes and stop this installation from replying on Telegram.'}</p></div>
    ${r.daemon||(r.removal?.action==='detach'&&!['done','rolled_back'].includes(r.removal.status))?`<div class="removal-choice"><button class="text-button" data-instance="${r.id}" data-removal="detach" ${state.active?'disabled':''}>Remove the daemon, keep Hermes…</button><p>Keep Hermes available, without the Daemons memory and continuity.</p></div>`:''}`:'<p class="small">Automatic removal is not available for this installation. Nothing will be deleted.</p>'}</details></div></details>`;
}
function daemonCard(group) {
  const rows=group.hosts,first=rows[0],name=group.name||locale.t('daemon.unnamed',language);
  const disabled=state.instanceBusy||state.active;
  const manage=r=>`<button class="button primary" data-daemon-manage="${r.id}" ${disabled||!r.daemon.memory?'disabled':''}><span>${rows.length>1?'Manage via':'Manage'}</span>${rows.length>1?' '+hostName(r):''}</button>`;
  const health=r=>`<button class="text-button" data-health="${r.id}" ${state.instanceBusy?'disabled':''}><span>Health</span>${rows.length>1?' · '+hostName(r):''}</button>`;
  const setups=(state.inventory?.setups||[]).filter(x=>rows.some(r=>r.id===x.instanceId)&&x.hasDiagnostic);
  // The app adds OpenClaw to a Hermes daemon; other combinations are not offered.
  const addable=rows.some(r=>r.host==='openclaw')?null:rows.find(r=>r.host==='hermes');
  // A Telegram problem reported by the host (main.cjs daemon-channels).
  const restart=rows.map(r=>restarts[r.id]).find(Boolean);
  const channel=restart?null:rows.map(r=>({r,state:state.channels?.[r.id]})).find(x=>['disconnected','token-rejected','conflict'].includes(x.state));
  const progress=restart?`<p class="daemon-restart" role="status">${restart.phase==='restarting'?'<span class="spinner" aria-hidden="true"></span>':''}${escape(locale.t('daemons.restart.'+restart.phase,language))}</p>`:'';
  const alert=channel?`<div class="daemon-alert" data-daemon-alert="${channel.r.id}"><p>${locale.t('daemons.channel.'+channel.state,language)}</p>
      ${channel.state==='disconnected'&&channel.r.restartable?`<button class="button primary" data-daemon-restart="${channel.r.id}" ${disabled?'disabled':''}>${locale.t('health.v2.restart',language)}</button>`:''}</div>`:'';
  return `<article class="card daemon-card" data-daemon-card="${group.id}"><h2 class="daemon-title" data-original>${escape(name)}</h2>
    <p class="daemon-hosts"><span>${rows.length>1?'Hosts':'Host'}</span>: <span data-original>${rows.map(hostName).join(' · ')}</span></p>${progress}${alert}
    <div class="daemon-card-actions">${rows.length===1?manage(first)+health(first):`<details class="host-choices"><summary>Manage</summary><div>${rows.map(manage).join('')}</div></details><details class="host-choices"><summary>Health</summary><div>${rows.map(health).join('')}</div></details>`}
      ${addable?`<button class="text-button" data-host-add="${addable.id}" ${disabled?'disabled':''}>Add a host…</button>`:''}
      ${first.daemon.memory?`<button class="text-button" data-instance="${first.id}" data-instance-open="memory">Open memory ↗</button>`:''}</div>
    ${rows.map(hostControls).join('')}
    ${setups.map(x=>`<details class="instance-details"><summary>Installation report</summary>${setupDate(x)}<button class="text-button" data-setup-report="${x.id}">View diagnostic</button></details>`).join('')}
  </article>`;
}
function setupDate(setup) {return `<p class="hint"><span>Started</span>: <span data-original>${escape(new Date(setup.createdAt).toLocaleString(language==='fr'?'fr-FR':'en-GB'))}</span></p>`;}
function setupCard(setup) {
  const row=state.inventory?.instances.find(r=>r.id===setup.instanceId);
  const name=row?.daemon?.name||locale.t('daemons.new',language);
  return `<article class="card setup-card" data-setup-card="${setup.id}"><h2 class="daemon-title" data-original>${escape(name)}</h2>
    <p class="daemon-hosts"><span>Host</span>: ${setup.host==='openclaw'?'OpenClaw':'Hermes Agent'}</p><p class="small">${setup.status==='saved'?'Setup to finish':setup.status==='installed'?'Installed files retained':'Installation to check'}</p>${setupDate(setup)}
    <div class="daemon-card-actions">${setup.status==='saved'?`<button class="button" data-setup-resume="${setup.id}" ${state.active||state.instanceBusy?'disabled':''}>Resume setup</button>`:''}
    ${setup.discardSupported&&setup.status!=='installed'?`<button class="text-button" data-setup-discard="${setup.id}" ${state.active||state.instanceBusy?'disabled':''}>Delete unfinished installation…</button>`:''}
    ${setup.hasDiagnostic?`<button class="text-button" data-setup-report="${setup.id}">View diagnostic</button>`:''}</div></article>`;
}
function instanceBody() {
  const inventory=state.inventory,rows=inventory?.instances||[];
  const pending=(inventory?.setups||[]).filter(x=>x.status!=='installed'||!rows.some(r=>r.id===x.instanceId&&r.daemon));
  const pendingHosts=new Set(pending.map(x=>x.instanceId).filter(Boolean));
  const groups=groupDaemons(rows.filter(r=>!pendingHosts.has(r.id)));
  return `<h1>My daemons</h1><p class="intro daemons-definition">Your daemon is your personal AI assistant. Its identity, its persistent memory and the thread of your conversations are designed to last for years. It relies on a host agent that gives it its ability to act. You can change host or model over time without losing the continuity of your experience.</p>
    ${updateReminder()}
    ${state.instanceBusy?'<div class="row busy" role="status"><span class="spinner"></span><span>Finding installations…</span></div>':''}
    ${inventory&&!inventory.servicesAvailable?'<p class="note">Service status is unavailable. The profiles below remain accessible.</p>':''}
    ${inventory?.registryWarning?'<p class="note">Saved instance connections could not be read. Connect an instance again before managing it.</p>':''}
    ${inventory?.missing?'<p class="note">A saved profile is unavailable. Its files have not been changed.</p>':''}
    ${inventory?.setupsWarning?'<p class="note">Saved setups could not be listed. Existing daemons remain accessible.</p>':''}
    ${groups.daemons.map(daemonCard).join('')}
    ${pending.map(setupCard).join('')}
    ${inventory&&!groups.daemons.length&&!pending.length?'<div class="card install-card"><h2>No daemon yet</h2><p>Install a daemon, or add an existing installation.</p><p class="hint">Choose Hermes Agent or OpenClaw for a separate private installation.</p></div>':''}
    ${groups.hosts.length?`<section class="hostless"><h2>Hosts without a daemon</h2><p class="hint">These hosts are available on this computer. Attaching a daemon to an existing host is not yet supported in this app.</p>${groups.hosts.map(r=>`<article class="card hostless-card"><h3>${hostName(r)}</h3><p class="small" data-original>${escape(r.profile.split(/[\\/]/).filter(Boolean).at(-1))}</p>${hostControls(r)}</article>`).join('')}</section>`:''}
    <p class="hint">An installation is missing? Select its profile folder with Add an existing installation. Remote computers and containers are not scanned.</p>`;
}
async function resumeSetup(id) {
  if(state.active||state.instanceBusy)return;
  state.resumeId=id;await action('start');
}
async function refreshChannels() {
  try {state.channels=await window.companion.channels();}catch{state.channels={};}
  settleRestarts();
}
async function refreshInstances(add=false) {
  if(state.instanceBusy)return;state.instanceBusy=true;state.error='';render();
  try {state.inventory=await (add?window.companion.addInstance():window.companion.instances());await refreshChannels();}
  catch(error){showError(error);}finally{state.instanceBusy=false;render();}
}
// A daemon whose host controls were enabled keeps the confirmed instance action;
// one the app created restarts like after a model change (main.cjs daemon-restart).
async function restartRow(id) {
  const row=state.inventory?.instances.find(r=>r.id===id);
  if(row?.managed)return window.companion.instanceAction(id,'restart');
  try{return await window.companion.restartDaemon(id);}catch{throw new Error(locale.t('daemons.restart-failed',language));}
}
// What a card says after Restart the daemon: restarting, then connecting until
// the host reports Telegram, then "reconnected" for a minute. A problem the
// host reports once the restart has settled brings the alert back.
const RESTART_SETTLE_MS=15000,RESTART_WATCH_MS=150000,RECONNECTED_MS=60000,OPENCLAW_RECHECK_MS=30000;
async function restartWithStatus(id) {
  restarts[id]={phase:'restarting',at:Date.now()};render();
  try{await restartRow(id);restarts[id]={phase:'connecting',at:Date.now(),checked:Date.now()};}
  catch(error){delete restarts[id];throw error;}
}
async function restartDaemon(id) {
  if(state.instanceBusy)return;state.instanceBusy=true;state.error='';render();
  try{await restartWithStatus(id);state.inventory=await window.companion.instances();await refreshChannels();}
  catch(error){showError(error);}finally{state.instanceBusy=false;render();scheduleChannels();}
}
function settleRestarts(now=Date.now()) {
  for(const [id,r] of Object.entries(restarts)) {
    const channel=state.channels?.[id];
    if(r.phase==='connecting'){
      if(channel==='connected')restarts[id]={phase:'connected',at:now};
      else if(now-r.at>RESTART_WATCH_MS||(now-r.at>RESTART_SETTLE_MS&&['disconnected','token-rejected','conflict'].includes(channel)))delete restarts[id];
    } else if(r.phase==='connected'&&now-r.at>RECONNECTED_MS)delete restarts[id];
  }
}
// The daemons page reads the hosts' Telegram state again on its own: every
// 5 seconds while a restart is followed, every 30 seconds otherwise, also
// while the window is covered (macOS then reports the page as hidden), so the
// card is current when the window comes back. It redraws only when something
// changed, so an open section stays open.
function scheduleChannels() {
  clearTimeout(channelTimer);
  channelTimer=setTimeout(watchChannels,Object.values(restarts).some(r=>r.phase!=='restarting')?5000:30000);
}
async function watchChannels() {
  try {
    if(state.page!=='instances'||state.instanceBusy||state.active)return;
    const now=Date.now(),openclaw=new Set((state.inventory?.instances||[]).filter(r=>r.host==='openclaw').map(r=>r.id));
    const live=Object.entries(restarts).filter(([id,r])=>r.phase==='connecting'&&openclaw.has(id)&&now-r.checked>=OPENCLAW_RECHECK_MS).map(([id])=>id);
    for(const id of live)restarts[id].checked=now;
    const before=JSON.stringify([state.channels,restarts]);
    try {state.channels=await window.companion.channels(live);}catch{return;}
    settleRestarts();
    if(JSON.stringify([state.channels,restarts])!==before&&state.page==='instances'&&!state.instanceBusy)render();
  } finally {scheduleChannels();}
}
scheduleChannels();
async function instanceAction(id,action) {
  if(state.instanceBusy)return;state.instanceBusy=true;state.error='';render();
  try{await window.companion.instanceAction(id,action);state.inventory=await window.companion.instances();await refreshChannels();}
  catch(error){showError(error);}finally{state.instanceBusy=false;render();}
}

function daemonEdited() {
  rememberDraft();state.daemonMessage='';state.error='';updateDaemonFeedback();
}
function updateDaemonFeedback() {
  const status=document.querySelector('#daemon-status'),error=document.querySelector('#daemon-error');
  if(!status||!error)return;
  const busy=state.daemonBusy,dirty=daemonDirty();
  const saved=state.daemonMessage==='Settings saved. The daemon reads them when composing its next conversation turn.';
  const key=busy?(state.daemon?'daemon.feedback.saving':'daemon.feedback.loading'):dirty?'daemon.feedback.unsaved':'daemon.feedback.clean';
  const message=saved?locale.t('daemon.feedback.saved',language):state.daemonMessage?locale.message(state.daemonMessage,language):locale.t(key,language);
  status.innerHTML=state.error?'':`${busy?'<span class="spinner" aria-hidden="true"></span>':''}<span>${escape(message)}</span>`;
  error.innerHTML=errorBody(state.error);locale.localizeElement(error,language);
  document.querySelector('#daemon-form')?.setAttribute('aria-busy',String(busy));
  const save=document.querySelector('[data-action=daemon-save]');
  if(save) {
    save.disabled=busy||!dirty;
    save.textContent=locale.t(busy?'daemon.feedback.saving':saved&&!dirty?'daemon.feedback.savedButton':'daemon.save',language);
  }
}
function daemonDirty() {
  return !!state.daemon && !!state.daemonDraft && Object.keys(state.daemon.values).some(k=>state.daemonDraft[k]!==state.daemon.values[k]);
}
function discardDaemonDraft() {
  rememberDraft();
  return !daemonDirty() || window.confirm(locale.t('daemon.discard',language));
}
function daemonAccounts(d) {
  if(!d.accounts?.length)return '';
  return `<details class="daemon-accounts"><summary>Accounts and channels</summary><ul>${d.accounts.map(a=>`<li><span data-original>${a.platform==='telegram'?'Telegram':'OpenClaw'} · ${escape(a.user_id)}</span><p class="small">${a.addressing?'These preferences apply here.':'Detected on a host using this memory.'}</p></li>`).join('')}</ul>
    <p class="hint">Additional accounts can use the same daemon. Linking their addressing preferences is not available in this version.</p></details>`;
}
function daemonBody() {
  const d=state.daemon,v=state.daemonDraft||{};
  if(!d)return '<h1>Daemon settings</h1>';
  const field=(key,label,editable)=>`<label class="field-label" for="daemon-${key}">${label}</label><input class="input" id="daemon-${key}" data-daemon-field="${key}" value="${escape(v[key]||'')}" maxlength="40" autocomplete="off" spellcheck="false" ${state.daemonBusy||!editable?'disabled':''}>`;
  return `<div class="eyebrow">Daemon settings</div><h1 data-original>${escape(v.name||locale.t('daemon.unnamed',language))}</h1>
    <p class="intro"><span>Host agent</span> <strong data-original>${d.host==='hermes'?'Hermes Agent':'OpenClaw'}</strong></p>
    ${d.shared.length?`<div class="note"><b>This daemon is shared by several hosts.</b><p>Changes to its name apply to every host using this memory.</p><details><summary>Hosts using this daemon</summary><ul>${[{host:d.host,profile:d.profile},...d.shared].map(h=>`<li><span data-original>${h.host==='hermes'?'Hermes Agent':'OpenClaw'} · ${escape(h.profile)}</span></li>`).join('')}</ul></details></div>`:''}
    ${!d.canEditName?'<p class="note">The configured daemon version could not be verified for editing. You can still open its memory folder.</p>':''}
    <form id="daemon-form" data-review="${d.token}"><fieldset class="daemon-fields"><legend>Daemon identity</legend>
      ${field('name','Daemon name',d.canEditName)}
      <label class="field-label" for="daemon-gender">Grammatical agreement for the daemon</label>
      <select class="input" id="daemon-gender" data-daemon-field="gender" ${state.daemonBusy||!d.canEditName?'disabled':''}>${[['','daemon.gender.choose'],['feminine','daemon.gender.feminine'],['masculine','daemon.gender.masculine']].map(([value,key])=>`<option value="${value}" ${v.gender===value?'selected':''}>${escape(locale.t(key,language))}</option>`).join('')}</select>
    </fieldset><fieldset class="daemon-fields"><legend>How it addresses you</legend>
      ${field('preferred_name','Your preferred name',d.canEditUser)}
      ${!d.canEditUser?`<p class="note">${d.userReason==='unsupported'?escape(locale.t('daemon.user.unsupported',language)):'No single user account is linked to these settings. Tell your daemon your preferences in conversation.'}</p>`:'<p class="hint">These are your setup preferences. Explicit corrections you have given in conversation remain authoritative.</p>'}
      ${daemonAccounts(d)}
    </fieldset></form>
    ${daemonUpdateSection(v)}
    ${providerSection()}
    ${hostChangeSection(d,v)}
    <section class="daemon-memory"><h2>Memory</h2><p>Your memories are stored in this folder.</p><p class="memory-path" data-original>${escape(d.memory)}</p>
      <div class="links"><button class="text-button" data-instance="${d.id}" data-instance-open="memory">Open memory folder ↗</button>${button('Copy path','daemon-copy')}</div>
      <div class="obsidian-help"><h3>Obsidian</h3><p>${escape(locale.t('obsidian.benefit',language))}</p>
      ${obsidianInfo?.available?`<button class="button" data-obsidian-open ${obsidianBusy?'disabled aria-busy="true"':''}>${escape(locale.t(obsidianBusy?'obsidian.busy':obsidianInfo.registered?'obsidian.open':'obsidian.configure',language))}</button>`:`<p>${escape(locale.t('obsidian.unavailable',language))}</p>`}
      ${obsidianInfo?.available&&!obsidianInfo.registered?`<p class="hint">${escape(locale.t('obsidian.first',language))}</p>`:''}
      <div class="links"><button class="text-button" data-project-link="obsidian">Download Obsidian ↗</button><button class="text-button" data-obsidian-refresh>${escape(locale.t('health.refresh',language))}</button></div>
      <p role="status" class="row">${obsidianBusy?'<span class="spinner" aria-hidden="true"></span>':''}${obsidianMessage?escape(locale.t(obsidianMessage,language)):''}</p></div>
      <div class="backup-help"><h3>Backup</h3><p>Save an encrypted copy of this daemon’s memory. You can install a new daemon from it, on this computer or another one.</p>${button('Back up memory…','daemon-backup',false,state.daemonBusy)}${dailyBackup()}</div></section>
    <div class="links">${button('Reload saved settings','daemon-reload',false,state.daemonBusy)}</div>`;
}
// Shown only when the app can bring this daemon to its runtime, or must say why
// not; a daemon already on it shows nothing (runtime/upgrade.py check).
const UPDATE_PENDING=['prepared','in_progress','failed','rollback_failed','command_recovery'];
const UPDATE_REFUSALS={'protected-runtime':'daemonUpdate.engine','portability':'daemonUpdate.portability','attached-hosts':'daemonUpdate.attachedHosts',
  'services-changed':'hostAdd.servicesChanged','hermes-profile':'hostAdd.hermesProfile','openclaw-profile':'daemonUpdate.openclawProfile','host-config':'hostAdd.hostConfig'};
function daemonUpdateSection(v) {
  const u=daemonUpdate,name=v.name||locale.t('daemon.unnamed',language),t=key=>escape(locale.t(key,language,{name}));
  if(!u||u.status==='current')return '';
  const start=label=>`<div class="links"><button class="button" data-daemon-update ${state.daemonBusy||state.active?'disabled':''}>${t(label)}</button></div>`;
  const body=u.status==='available'?`<p>${t('daemonUpdate.available')}</p><p class="hint">${t('daemonUpdate.scope')}</p>${start('daemonUpdate.start')}`
    :UPDATE_PENDING.includes(u.status)?`<p class="note">${t('hostAdd.interrupted')}</p>${start('daemonUpdate.restore')}`
    :u.status==='refused'?`<p>${t(UPDATE_REFUSALS[u.code]||'daemonUpdate.unavailable')}</p>`
    :`<p class="hint">${t('daemonUpdate.unavailable')}</p>`;
  return `<section class="daemon-update"><h2>${t('daemonUpdate.title')}</h2>${body}</section>`;
}
async function startDaemonUpdate(id) {
  if(state.active||state.instanceBusy||state.daemonBusy)return;
  hostAdditionState.id=id;
  Object.assign(state,{page:'setup',mode:'update-daemon',operation:'setup',active:true,result:null,messages:[],guidance:[],links:[],prompt:null,phase:'installation',error:'',modelCatalog:null,chatgptCode:'',chatgptStage:'',activity:'Preparing the update…'});
  render();
  try{await window.companion.startDaemonUpdate(id);}catch(error){state.active=false;showError(error);}
}
function hostChangeSection(d,v) {
  if(!['hermes','openclaw'].includes(d.host))return '';
  const [from,to]=d.host==='hermes'?['Hermes Agent','OpenClaw']:['OpenClaw','Hermes Agent'];
  return `<section class="daemon-host"><h2>${escape(locale.t('change.title',language))}</h2>
    ${d.shared.length?`<p>${escape(locale.t('change.shared',language))}</p>`:`<p>${escape(locale.t('change.intro',language,{name:v.name||locale.t('daemon.unnamed',language),from,to}))}</p>
    <p class="hint">${escape(locale.t('change.steps',language,{from,to}))}</p>
    <div class="links">${button(escape(locale.t('change.start',language,{to})),'daemon-host-change',false,state.daemonBusy||state.active)}</div>`}</section>`;
}
function dailyBackup() {
  if(!google.configured||!daemonBackup)return '';
  const a=daemonBackup,when=a.lastSuccess?new Date(a.lastSuccess).toLocaleString(language==='fr'?'fr-FR':'en-GB',{dateStyle:'medium',timeStyle:'short'}):'';
  return `<h3>Daily backup to Google Drive</h3>${a.enabled
    ?`<p>On. A backup is sent every day while the app is running; the last seven are kept.</p>${when?`<p>Last backup: ${escape(when)}</p>`:''}${a.lastError?`<p class="note">${escape(errorText(a.lastError))}</p>${codeStatus(a.lastError)}`:''}${button('Turn off daily backup','daemon-backup-off',false,state.daemonBusy)}`
    :`<p>Off.</p>${button('Turn on daily backup…','daemon-backup-auto',false,state.daemonBusy||!google.connected)}${google.connected?'':'<p class="hint">Connect Google Drive in Settings first.</p>'}`}`;
}
const PROVIDER_NAMES={mistral_api:'Mistral AI',openai_api:'OpenAI',anthropic_api:'Anthropic',xai_api:'SpaceXSI'};
function providerError(value) {
  const clean=String(value||'').replace(/^Error invoking remote method '[^']+': Error: /,'');
  if(/^(provider|attachment)\./.test(clean)){const [code,detail='']=clean.split('|');let text;
    try{text=locale.t(code,language);}catch{text=locale.t('provider.unavailable',language);}
    return escape(detail?text+' ('+detail+')':text);}
  // Fixed provider messages from the key check are already in the catalog.
  return escape(locale.message(clean,language))+serviceStatus(clean);
}
function providerSection() {
  const p=providerInfo,s=providerState,t=key=>escape(locale.t(key,language));
  if(!p)return `<section class="daemon-model"><h2>${t('provider.title')}</h2><p class="hint">${t('provider.unavailableInfo')}</p></section>`;
  const api=Boolean(PROVIDER_NAMES[p.kind]);
  const name=api?`<span data-original>${PROVIDER_NAMES[p.kind]}</span>`:t(p.kind==='codex_cli'?'provider.kind.subscription':'provider.kind.compatible');
  const status=s.busy?`<span class="spinner" aria-hidden="true"></span>${t(s.busy)}`:s.done?escape(locale.t(s.done.key,language,{model:s.done.model})):'';
  const checked=s.checked?`<p class="${s.checked.available?'':'note'}">${t(s.checked.available?'provider.valid':'provider.validMissing')}</p>`:'';
  const switchButton=p.switchable&&!s.editing?`<button class="button" data-provider-switch ${s.busy||state.active?'disabled':''}>${t('provider.switch')}</button>`:'';
  const options=(s.models||[p.model]).map(m=>`<option value="${escape(m)}" ${m===(s.model||p.model)?'selected':''}>${escape(m)}</option>`).join('');
  const form=s.editing?`<form id="provider-form" class="daemon-fields">
      <label class="field-label" for="provider-key">${t('provider.newKey')}</label>
      <input class="input" id="provider-key" type="password" value="${escape(s.key||'')}" maxlength="1500" autocomplete="off" spellcheck="false" ${s.busy?'disabled':''}>
      <div class="links"><button type="button" class="text-button" data-provider-models ${s.busy?'disabled':''}>${t('provider.loadModels')}</button></div>
      <label class="field-label" for="provider-model">${t('provider.choose')}</label>
      <select class="input" id="provider-model" ${s.busy?'disabled':''}>${options}</select>
      <p class="hint">${t('provider.saveHint')}</p><p class="hint">${t('provider.voiceHint')}</p>
      <div class="links"><button type="submit" class="button primary" ${s.busy?'disabled':''}>${t('provider.save')}</button><button type="button" class="button" data-provider-cancel ${s.busy?'disabled':''}>${t('instances.cancel')}</button></div>
    </form>`:'';
  return `<section class="daemon-model"><h2>${t('provider.title')}</h2><p>${t('provider.intro')}</p>
    <dl class="about-meta model-meta"><div><dt>${t('provider.provider')}</dt><dd>${name}</dd></div><div><dt>${t('provider.modelLabel')}</dt><dd data-original>${escape(p.model)}</dd></div>${api?`<div><dt>${t('provider.keyLabel')}</dt><dd>${t(p.key?'provider.key.saved':'provider.key.missing')}</dd></div>`:''}</dl>
    ${p.recovered?`<p class="note">${t('provider.recovered')}</p>`:''}
    ${api?`${checked}
      ${s.editing?form:`<div class="links">${p.key?`<button class="button" data-provider-check ${s.busy?'disabled':''}>${t('provider.check')}</button>`:''}${p.editable?`<button class="button" data-provider-edit ${s.busy?'disabled':''}>${t('provider.edit')}</button>`:''}${switchButton}</div>`}
      ${p.editable||p.switchable?'':`<p class="hint">${t('provider.notEditable')}</p>`}`
      :`<p class="hint">${t(p.kind==='codex_cli'?'provider.subscription':'provider.compatibleHint')}</p>${switchButton?`<div class="links">${switchButton}</div>`:''}`}
    ${p.switchable&&!s.editing?`<p class="hint">${t('provider.switchHint')}</p>`:''}
    ${s.error?`<p class="error" role="alert">${providerError(s.error)}</p>`:''}
    <p role="status" class="row">${status}</p></section>`;
}
async function loadProvider(id) {
  providerState={};providerInfo=null;
  providerInfo=await window.companion.providerStatus(id).catch(()=>null);
}
async function providerAction(work,busy) {
  const id=state.daemon?.id;if(!id||providerState.busy)return;
  Object.assign(providerState,{busy,error:'',done:null});render();
  try{await work(id);}catch(error){if(state.daemon?.id===id)providerState.error=error.message;}
  finally{if(state.daemon?.id===id){providerState.busy='';render();}}
}
function bindProvider() {
  const keyInput=document.querySelector('#provider-key'),modelInput=document.querySelector('#provider-model');
  keyInput?.addEventListener('input',()=>{providerState.key=keyInput.value;});
  modelInput?.addEventListener('change',()=>{providerState.model=modelInput.value;});
  document.querySelector('[data-provider-check]')?.addEventListener('click',()=>providerAction(async id=>{
    providerState.checked=await window.companion.checkProvider(id,null);
  },'provider.checking'));
  document.querySelector('[data-provider-edit]')?.addEventListener('click',()=>{Object.assign(providerState,{editing:true,key:'',model:providerInfo.model,models:null,error:'',done:null,checked:null});render();document.querySelector('#provider-key')?.focus();});
  document.querySelector('[data-provider-switch]')?.addEventListener('click',()=>{const id=state.daemon?.id;if(id)startProviderSwitch(id);});
  document.querySelector('[data-daemon-update]')?.addEventListener('click',()=>{const id=state.daemon?.id;if(id)startDaemonUpdate(id);});
  document.querySelector('[data-provider-cancel]')?.addEventListener('click',()=>{providerState={};render();});
  document.querySelector('[data-provider-models]')?.addEventListener('click',()=>providerAction(async id=>{
    const result=await window.companion.checkProvider(id,providerState.key?.trim()||null);
    providerState.models=result.models;
    if(!result.models.includes(providerState.model))providerState.model=result.models[0];
  },'provider.loading'));
  document.querySelector('#provider-form')?.addEventListener('submit',event=>{event.preventDefault();providerAction(async id=>{
    const model=providerState.model||providerInfo.model,key=providerState.key?.trim()||null;
    const result=await window.companion.changeProvider(id,providerInfo.revision,model,key);
    providerInfo=await window.companion.providerStatus(id).catch(()=>providerInfo);
    providerState={done:result.status==='unchanged'?{key:'provider.unchanged'}:{key:result.restarted===false?'provider.savedStopped':'provider.saved',model}};
  },'provider.saving');});
}
function errorText(code) { try {return locale.t(code,language);} catch {return locale.t('backup.unavailable',language);} }
function backupBody() {
  const r=backupState.result;
  const field=(id,label,value)=>`<label class="field-label" for="${id}">${label}</label><input class="input" id="${id}" type="password" value="${escape(value)}" maxlength="256" autocomplete="new-password" spellcheck="false" ${backupState.busy?'disabled':''}>`;
  if(r)return `<div class="eyebrow">Backup</div><h1>Backup saved</h1>
    ${r.automatic&&r.backgroundEnabled?'<p class="note">The app now keeps running in the background, so daily backups continue with its window closed. You can change this in Settings.</p>':''}${r.automatic?'<p class="intro">Daily backup is on. The first one is being sent now.</p>':r.drive?'<p class="intro">Sent to Google Drive, in the Daemons / Backups folder.</p>':`<p class="intro">Encrypted file</p><p class="memory-path" data-original>${escape(r.file)}</p>`}${r.automatic?'':`<p>${escape(String(r.files))} memory files.</p>`}
    ${r.failedTurns?`<div class="note"><b>Some recent turns are missing.</b><p>${escape(String(r.failedTurns))} recent conversation turns had not been filed into memory yet. Their text is in this backup, but what they taught your daemon is not.</p></div>`:''}
    <div class="note"><b>Keep your password.</b><p>Daemons cannot recover it. Without it, this backup cannot be opened.</p></div>`;
  return `<div class="eyebrow">Backup</div><h1>${backupState.mode==='automatic'?'Daily backup to Google Drive':'Back up this daemon’s memory'}</h1><p class="intro"><span>Daemon</span> <strong data-original>${escape(backupState.name||locale.t('daemon.unnamed',language))}</strong></p>
    <p class="intro">The backup contains your notes, the daemon’s identity, voice and conversation journal. It contains no password, key or account, and no reminder will run again from it.</p>
    <p>Your daemon keeps running. If it is still filing a recent conversation into its memory, the backup waits for it, up to a few minutes.</p>
    ${backupState.mode==='automatic'?'<p>Every day, while the app is running, even in the background, an encrypted backup is sent to your Google Drive. The last seven daily backups are kept; your manual backups are never deleted.</p>':google.configured?`<fieldset class="segment"><legend class="sr-only">Destination</legend><label><input type="radio" name="backup-destination" value="file" data-backup-destination ${backupState.destination==='file'?'checked':''} ${backupState.busy?'disabled':''}>Save to a file</label><label><input type="radio" name="backup-destination" value="drive" data-backup-destination ${backupState.destination==='drive'?'checked':''} ${backupState.busy||!google.connected?'disabled':''}>Send to Google Drive</label></fieldset>${google.connected?'':'<p class="hint">Connect Google Drive in Settings to send backups there.</p>'}`:''}
    <form id="backup-form" class="daemon-fields">${field('backup-password','Backup password (at least 12 characters)',backupState.password)}${field('backup-confirm','Confirm the password',backupState.confirm)}</form>
    <p class="hint">Daemons cannot recover this password. Keep it somewhere safe.</p>
    <p role="status" class="row">${backupState.busy?'<span class="spinner" aria-hidden="true"></span>Saving an encrypted copy…':''}</p>`;
}
async function restoreFromDrive(id) {
  if(restoreState.busy)return;
  const item=(restoreState.drive||[]).find(b=>b.id===id);if(!item)return;
  restoreState.busy=true;state.error='';render();
  try {await window.companion.chooseBackup(null,id);Object.assign(restoreState,{file:driveLabel(item),step:'password',password:''});}
  catch(error){showError(error);}
  finally {restoreState.busy=false;render();}
}
function driveLabel(b) {
  const when=new Date(b.createdAt).toLocaleString(language==='fr'?'fr-FR':'en-GB',{dateStyle:'medium',timeStyle:'short'});
  return `${b.name||'Daemon'} · ${when} · ${Math.max(1,Math.round(b.bytes/1024))} KB`;
}
function restoreBody() {
  if(restoreState.step==='source')return `<div class="eyebrow">Install a daemon</div><h1>Install from a backup</h1>
    <p class="intro">Choose a backup from your Google Drive, or a backup file on this computer.</p>
    <h2>Google Drive</h2>${restoreState.drive===null?'<p role="status" class="row"><span class="spinner" aria-hidden="true"></span>Loading your Google Drive backups…</p>':restoreState.drive.length?`<ul class="backup-list">${restoreState.drive.map(b=>`<li><span data-original>${escape(driveLabel(b))}</span> <span>${b.kind==='automatic'?'Daily':'Manual'}</span> <button class="text-button" data-restore-drive="${escape(b.id)}" ${restoreState.busy?'disabled':''}>Use this backup</button></li>`).join('')}</ul>`:'<p>No backup in your Google Drive yet.</p>'}
    <h2>File</h2><div class="links">${button('Choose a backup file…','restore-file',false,restoreState.busy)}</div>`;
  return `<div class="eyebrow">Install a daemon</div><h1>Install from a backup</h1>
    <p class="intro">Backup file</p><p class="memory-path" data-original>${escape(restoreState.file)}</p>
    <p>A new daemon is installed with this backup’s name, identity and memories. You then choose its host, connect a model and give it a Telegram bot. Installing from a backup never changes the original daemon.</p>
    <fieldset class="restore-kind"><legend>What happens to the original daemon?</legend>
      <label><input type="radio" name="restore-kind" value="copy" data-restore-kind ${restoreState.move?'':'checked'} ${restoreState.busy?'disabled':''}><span><b>Keep it.</b> The new daemon is a copy. Scheduled reminders stay with the original, so none is sent twice.</span></label>
      <label><input type="radio" name="restore-kind" value="move" data-restore-kind ${restoreState.move?'checked':''} ${restoreState.busy?'disabled':''}><span><b>Move it here.</b> The original daemon is stopped or removed. Its upcoming reminders move too, and you can reuse its Telegram bot.</span></label>
    </fieldset>
    <form id="restore-form" class="daemon-fields"><label class="field-label" for="restore-password">Backup password</label><input class="input" id="restore-password" type="password" value="${escape(restoreState.password)}" maxlength="256" autocomplete="current-password" spellcheck="false" ${restoreState.busy?'disabled':''}></form>
    <p role="status" class="row">${restoreState.busy?'<span class="spinner" aria-hidden="true"></span>Opening the backup…':''}</p>`;
}
async function backupAction(name) {
  if(document.querySelector('#backup-password')){backupState.password=document.querySelector('#backup-password').value;backupState.confirm=document.querySelector('#backup-confirm').value;}
  if(name==='backup-back'){const id=backupState.id;Object.assign(backupState,{busy:false,result:null,password:'',confirm:''});await openDaemon(id);return;}
  if(backupState.password.length<12){showError(new Error('backup.passwordLength'));return;}
  if(backupState.password!==backupState.confirm){showError(new Error('backup.passwordMismatch'));return;}
  const password=backupState.password;
  backupState.busy=true;state.error='';render();
  try {const result=backupState.mode==='automatic'?{...await window.companion.automaticBackup(backupState.id,true,password),saved:true,automatic:true}
      :backupState.destination==='drive'?await window.companion.backUpToDrive(backupState.id,password):await window.companion.backUpMemory(backupState.id,password);
    if(result.backgroundEnabled)backgroundMode={...backgroundMode,enabled:true,openAtLogin:true};
    if(result.saved){backupState.result=result;backupState.password=backupState.confirm='';}
  } catch(error) {showError(error);}
  finally {backupState.busy=false;render();}
}
async function restoreAction(name) {
  if(name==='restore-back'){Object.assign(restoreState,{file:'',busy:false,password:''});state.page='instances';state.error='';await refreshInstances();return;}
  if(name==='restore-choose'||name==='restore-file'){
    if(name==='restore-choose'){await refreshGoogle();
      if(google.connected){Object.assign(restoreState,{file:'',busy:true,password:'',step:'source',drive:null});state.page='restore';state.error='';render();
        try{restoreState.drive=await window.companion.driveBackups();}catch(error){restoreState.drive=[];showError(error);}finally{restoreState.busy=false;render();}return;}}
    try {const chosen=await window.companion.chooseBackup();
      if(!chosen.chosen)return;
      Object.assign(restoreState,{file:chosen.file,busy:false,password:'',step:'password'});state.page='restore';state.error='';render();
    } catch(error) {showError(error);}
    return;
  }
  if(document.querySelector('#restore-password'))restoreState.password=document.querySelector('#restore-password').value;
  if(!restoreState.password){showError(new Error('backup.passwordLength'));return;}
  const password=restoreState.password;
  restoreState.busy=true;state.error='';
  // Reset the setup view before starting: its first events, a quick failure
  // included, can arrive before the start call returns and must not be erased.
  Object.assign(state,{active:true,result:null,messages:[],guidance:[],links:[],prompt:null,activity:'Preparing your private setup…'});
  render();
  try {
    await window.companion.restoreBackup(password,restoreState.move);
    restoreState.password='';restoreState.busy=false;
    draft=null;state.page='setup';state.mode='setup';state.operation='setup';state.savedSetup='saved';state.overview=null;state.setupStarted=true;
    render();
  } catch(error) {restoreState.busy=false;state.active=false;showError(error);}
}
async function openDaemon(id) {
  if(state.daemonBusy || state.active)return;
  obsidianInfo=null;obsidianMessage='';daemonUpdate=null;
  rememberDraft();state.page='daemon';state.daemon=null;state.daemonDraft=null;state.daemonPronounChoice=null;state.daemonCustomPronouns='';state.daemonPreferencesOpen=null;state.daemonBusy=true;state.error='';state.daemonMessage='';render();
  try {state.daemon=await window.companion.daemonSettings(id);state.daemonDraft={...state.daemon.values};obsidianInfo=await window.companion.obsidianStatus(id).catch(()=>null);daemonBackup=null;await refreshGoogle(id);await loadProvider(id);daemonUpdate=await window.companion.daemonUpdateStatus(id).catch(()=>null);}
  catch(error){state.error=error.message;}
  finally {state.daemonBusy=false;render();}
}
async function saveDaemon() {
  rememberDraft();
  if(state.daemonBusy||!state.daemon?.canEditName||!daemonDirty())return;
  const values={...state.daemonDraft};
  if(!values.name.trim()||!values.gender||(state.daemon.values.preferred_name&&!values.preferred_name.trim())) {
    state.error='Enter the daemon name and grammatical agreement. Keep a preferred name if one is already configured.';render();return;
  }
  state.daemonBusy=true;state.error='';state.daemonMessage='';render();
  try {
    const result=await window.companion.saveDaemon(state.daemon.id,state.daemon.token,values);
    if(result.cancelled)state.daemonMessage='Save cancelled. Your changes are still here.';
    else {state.daemon=result;state.daemonDraft={...result.values};state.daemonPronounChoice=null;state.daemonCustomPronouns='';state.daemonMessage='Settings saved. The daemon reads them when composing its next conversation turn.';}
  }catch(error){state.error=error.message;}
  finally {state.daemonBusy=false;render();}
}

function removalBody() {
  const p=removalState.plan;
  if(removalState.failure){
    const d=removalState.failure.diagnostic,tr=k=>escape(locale.t(k,language));
    return `<h1>${tr('removal.report.title')}</h1><p>${tr(removalState.failure.code)}</p>
      <p class="note">${tr(d.phase==='review'?'removal.report.unchanged':'removal.report.recovery')}</p>
      <p><b>${tr('removal.report.stage')}</b> ${tr('removal.stage.'+d.stage)}</p>
      <p class="small">${tr('removal.report.share')}</p>
      <pre data-original>${escape(JSON.stringify(d,null,2))}</pre>
      <div class="links">${button(tr('diagnostic.export'),'removal-export')}${button(tr('removal.report.retry'),'removal-retry')}</div>
      <p class="hint">${tr('diagnostic.privacy')}</p><p role="status">${escape(removalState.message)}</p>`;
  }
  if(!p)return '<h1>Review uninstall</h1>'+(removalState.busy?'<div class="row busy" role="status"><span class="spinner"></span><span>Checking ownership and services…</span></div>':'');
  const detach=p.action==='detach';
  const folder=(kind,label,copy=false)=>`<button class="text-button" data-action="removal-${kind}${copy?'-copy':''}">${label}</button>`;
  const openclaw=p.host==='openclaw';
  const title=p.done?(detach?'Daemon removed from Hermes':'Installation uninstalled'):(detach?'Remove the daemon, keep Hermes':openclaw?'Uninstall OpenClaw and the daemon':p.daemonAttached?'Uninstall Hermes and the daemon':'Uninstall Hermes');
  return `<section class="removal-review"><div class="eyebrow">Selected installation</div><p class="removal-target" data-original>${escape(p.installationName)}</p><h1>${title}</h1>
  <p class="removal-effect">${detach?'Hermes stays installed and can still reply on Telegram. It will no longer use the Daemons memory or maintain your daemon’s continuity.':openclaw?'OpenClaw and the daemon will no longer run in this installation. Their programs and background services are removed. This installation will no longer reply on Telegram.':p.daemonAttached?'Hermes and the daemon will no longer run in this installation. Their programs and background services are removed. This installation will no longer reply on Telegram.':'Hermes and its background services are removed. This installation will no longer reply on Telegram.'}</p>
  ${removalMemory(p,folder)}
  ${detach&&p.programs?.length&&!p.done?'<p class="note">The local memory search model and its server are removed too (about 640 MB). Hermes does not use them.</p>':''}
  ${p.addedHosts&&!p.done?`<p class="note"><b>${detach?'OpenClaw, added to this daemon, will be stopped and disconnected first. It stays installed.':'OpenClaw, added to this daemon, will be stopped and uninstalled too.'}</b></p>`:''}
  ${p.recovery?'<p class="note">An earlier removal did not finish. Recovery restores the previous installation when possible, or finishes only the program cleanup already confirmed. Memory stays in place.</p>':''}
  ${!p.done?`<div class="removal-confirmation"><label class="field-label" for="removal-name">${detach?'To remove only the daemon, type its name:':'To uninstall this installation, type its full name:'}</label><p class="removal-target" data-original>${escape(p.confirmationText)}</p><input class="input" id="removal-name" value="${escape(removalState.name)}" autocomplete="off" spellcheck="false" ${removalState.busy?'disabled':''}></div>`:''}
  <details class="removal-details"><summary>Installation details and retained files</summary><p class="small" data-original>${escape(p.profile)}</p><p>${openclaw?'The conversation history is also kept. The saved API key and Telegram bot token are deleted from this computer; the bot and the provider account themselves are not deleted.':'The conversation history and saved local sign-ins are also kept. The Telegram bot and provider accounts are not deleted.'}</p><p data-original>${escape(p.retainedProfile)}</p><p class="small" data-original>${escape(p.retainedSignIns||'')}</p>${folder('profile','Open installation folder ↗')}<p>Programs and background services removed:</p><ul>${[...p.programs,...p.services].map(v=>`<li class="small" data-original>${escape(v)}</li>`).join('')}</ul></details>
  ${removalState.busy?'<div class="row busy" role="status"><span class="spinner"></span><span>Applying the reviewed removal…</span></div>':''}
  ${removalState.message?`<p role="status">${escape(removalState.message)}</p>`:''}</section>`;
}
// The memory note; a vault Companion created for this daemon alone may also go
// to the trash, asked here and confirmed again before anything starts.
function removalMemory(p,folder) {
  const windows=backgroundMode.platform==='win32',links=`<p class="memory-path" data-original>${escape(p.memory)}</p><div class="links">${folder('memory','Open memory folder ↗')}${folder('memory','Copy path',true)}</div>`;
  if(p.done&&p.memoryRemoved===true)return `<div class="note removal-memory"><b>${windows?'Memory moved to the Recycle Bin.':'Memory moved to the Trash.'}</b><p>It can be recovered from there until it is emptied.</p><p class="memory-path" data-original>${escape(p.memory)}</p></div>`;
  const offered=p.memoryDeletable&&p.action==='uninstall'&&!p.done,chosen=offered&&removalState.deleteMemory;
  const size=Math.max(1,Math.round((p.memoryBytes||0)/1e6));
  return `<div class="note removal-memory ${chosen?'removal-memory-chosen':''}"><b>${chosen?(windows?'Your memory will be moved to the Recycle Bin.':'Your memory will be moved to the Trash.'):'Your memory is kept.'}</b>
    ${chosen?`<p>After the uninstall, this whole folder is moved there (${p.memoryFiles} files, about ${size} MB): memories, identity and conversation journal. It can be recovered until it is emptied; after that, only a backup can bring this daemon back. A second confirmation is asked before anything starts.</p>`:`<p>${offered?'Your memories and the daemon’s identity stay in this folder.':'Your memories and the daemon’s identity stay in this folder. Deleting them is a separate, manual action.'}</p>`}
    ${links}
    ${p.done&&p.memoryRemoved===false?'<p class="error">The memory could not be deleted. It is kept in this folder.</p>':''}
    ${offered?`<label class="check removal-memory-delete"><input type="checkbox" data-removal-memory ${chosen?'checked':''} ${removalState.busy?'disabled':''}><span>Also delete this daemon’s memory</span></label>`:''}</div>`;
}
async function openRemoval(id,action) {
  if(state.active||state.instanceBusy||removalState.busy)return;
  removalState.id=id;removalState.action=action;removalState.failure=null;removalState.deleteMemory=false;
  state.page='removal';state.error='';removalState.plan=null;removalState.name='';removalState.message='';removalState.busy=true;render();
  try{const result=await window.companion.reviewRemoval(id,action);if(result.blocked)removalState.failure=result;else removalState.plan=result;}
  catch(error){state.error=locale.t(error.message.replace(/^.*?(removal\.)/,'$1'),language);}
  finally{removalState.busy=false;render();}
}
async function removalAction(action) {
  const p=removalState.plan;
  if(action==='removal-back'){state.page='instances';state.error='';await refreshInstances();return;}
  if(action==='removal-retry'){await openRemoval(removalState.id,removalState.action);return;}
  if(action==='removal-export'){
    try{const result=await window.companion.exportDiagnostic('removal',removalState.id);removalState.message=locale.t(result.exported?'diagnostic.exported':'diagnostic.cancelled',language);render();}catch(error){showError(error);}return;
  }
  if(!p)return;
  if(action==='removal-confirm') {
    if(removalState.name!==p.confirmationText)return;
    removalState.busy=true;state.error='';render();
    try{const result=await window.companion.applyRemoval(p.id,p.token,removalState.name,removalState.deleteMemory&&p.memoryDeletable===true);if(result.cancelled)return;if(result.blocked){removalState.failure=result;removalState.plan=null;return;}removalState.plan=result;const prefs=await window.companion.preferences();state.savedSetup=prefs.setup||'none';state.overview=null;state.setupStarted=false;}
    catch(error){state.error=locale.t(error.message.replace(/^.*?(removal\.)/,'$1'),language);removalState.name='';removalState.plan=null;}
    finally{removalState.busy=false;render();}
  } else {
    try{await window.companion.removalFolder(p.id,p.token,action.includes('memory')?'memory':'profile',action.endsWith('-copy'));if(action.endsWith('-copy')){removalState.message='Memory path copied.';render();}}
    catch(error){showError(error);}
  }
}

function reportBody() {
  const health=state.page==='health',r=health?reportState.health:reportState.diagnostic;
  const tr=key=>escape(locale.t(key,language));
  const date=value=>value?escape(new Date(value).toLocaleString(language==='fr'?'fr-FR':'en-GB')):tr('health.noDate');
  const badge=status=>`<span class="status health-status-${status}">${tr('health.status.'+status)}</span>`;
  return `<section class="health-report"><div class="eyebrow">${tr(health?'health.eyebrow':'diagnostic.eyebrow')}</div><h1>${tr(health?'health.title':'diagnostic.title')}</h1>
    ${!health&&reportState.id?`<p data-original>${escape(state.inventory?.instances.find(i=>i.id===state.inventory?.setups?.find(x=>x.id===reportState.id)?.instanceId)?.daemon?.name||locale.t('daemons.new',language))}</p>${state.inventory?.setups?.find(x=>x.id===reportState.id)?setupDate(state.inventory.setups.find(x=>x.id===reportState.id)):''}`:''}
    ${health?`<p data-original>${escape(state.inventory?.instances.find(i=>i.id===reportState.id)?.daemon?.name||'')} · ${r?.host==='openclaw'?'OpenClaw':'Hermes Agent'}</p>`:''}<p class="intro">${tr(health?'health.intro':'diagnostic.intro')}</p>
    ${reportState.busy?`<div class="row busy" role="status"><span class="spinner"></span><span>${tr('health.loading')}</span></div>`:''}
    ${r?health?`${healthSummary(r,tr,date)}<p class="small">${tr('health.checked')} <span data-original>${date(r.checkedAt)}</span></p>
    <details class="health-technical" data-health-detail="technical" ${reportState.expanded.has('technical')?'open':''}><summary>${tr('health.technical')}</summary><p class="hint">${tr('health.technicalHint')}</p><div class="health-checks">${r.checks.map(c=>healthCard(c,tr,date,badge)).join('')}</div></details>`:
    `<p><b>${tr('diagnostic.status.'+r.status)}</b></p>${!r.saved?`<p class="error">${tr('diagnostic.unsaved')}</p>`:''}<p class="small">${tr('diagnostic.technical')}</p><ol class="diagnostic-events">${r.events.map(e=>`<li><div class="row"><b>${tr('diagnostic.phase.'+e.phase)}</b><span>${tr('diagnostic.status.'+e.status)}</span></div><p class="small">${tr('diagnostic.operation.'+e.operation)}${e.transaction_phase?' · '+tr('diagnostic.transaction.'+e.transaction_phase):''}</p>${e.timeout_seconds?`<p>${escape(locale.t('diagnostic.timeout',language,{seconds:e.timeout_seconds}))}</p>`:''}${e.host_stage?`<p class="small">${tr('diagnostic.hostStage.'+e.host_stage)}</p>`:''}${e.winerror!==undefined?`<p data-original>winerror: ${e.winerror}</p>`:''}${e.exit_code!==undefined?`<p data-original>exit_code: ${e.exit_code}</p>`:''}${e.auth_stage?`<p>${tr('chatgpt.stage.'+e.auth_stage)}</p>`:''}${e.model_catalog?modelCatalogBody(e.model_catalog):''}${e.error_code?`<code data-original>${escape(e.error_code)}${e.errno!==undefined?' ('+e.errno+')':''}</code>`:''}<p class="hint" data-original>${date(e.at)}</p></li>`).join('')}</ol>`:''}
    <p class="note">${tr('diagnostic.privacy')}</p><p role="status">${escape(reportState.message)}</p></section>`;
}
async function openReport(kind,id=null) {
  if(reportState.busy)return;
  rememberDraft();reportState.back=state.page;state.page=kind;reportState.expanded.clear();reportState.id=id;reportState.health=null;reportState.diagnostic=null;reportState.message='';
  await loadReport();
}
async function loadReport() {
  reportState.busy=true;state.error='';render();
  try {if(state.page==='health'){reportState.health=await window.companion.health(reportState.id);reportState.service=await modelService(reportState.id);}else reportState.diagnostic=await window.companion.diagnostic(reportState.id);}
  catch(error){state.error=error.message;}
  finally {reportState.busy=false;render();}
}
async function reportAction(name,key=null) {
  if(name==='report-back'){state.page=reportState.back==='health'||reportState.back==='diagnostic'?'instances':reportState.back;state.error='';render();return;}
  if(name==='report-refresh'){await loadReport();return;}
  if(name==='report-export') {
    reportState.busy=true;reportState.message='';render();
    try {const result=await window.companion.exportDiagnostic(state.page==='health'?'health':'setup',reportState.id,key);
      reportState.message=locale.t(result.exported?'diagnostic.exported':'diagnostic.cancelled',language);
    }catch(error){state.error=error.message;}
    finally {reportState.busy=false;render();}
  }
}

// The top bar says where you are and what is left to do; page actions stay in
// the footer and the main navigation in the sidebar.
function crumbs() {
  const t=key=>locale.t(key,language),instances=state.inventory?.instances||[];
  const nameOf=id=>instances.find(i=>i.id===id)?.daemon?.name||'';
  const home={label:t('topbar.daemons'),action:'instances'};
  const daemonId=state.page==='daemon'?state.daemon?.id:state.page==='health'?reportState.id:state.page==='backup'?backupState.id:state.page==='removal'?removalState.id:null;
  const daemonName=state.page==='daemon'?state.daemon?.values?.name||nameOf(daemonId):state.page==='backup'?backupState.name||nameOf(daemonId):nameOf(daemonId);
  const daemon={label:daemonName,daemon:daemonId};
  if(state.page==='settings')return [{label:t('topbar.settings')}];
  if(state.page==='about')return [{label:t('topbar.about')}];
  if(state.page==='instances')return [{label:t('topbar.daemons')}];
  if(state.page==='daemon')return [home,{label:daemonName}];
  if(state.page==='health')return [home,daemon,{label:t('topbar.health')}];
  if(state.page==='backup')return [home,daemon,{label:t('topbar.backup')}];
  if(state.page==='removal')return [home,daemon,{label:t('topbar.removal')}];
  if(state.page==='restore')return [home,{label:t('topbar.restore')}];
  if(state.page==='diagnostic'||state.page==='discard')return [home,{label:t(state.page==='discard'?'topbar.discard':'topbar.diagnostic')}];
  return [home,{label:t(state.mode==='add-host'?'topbar.addHost':state.mode==='change-provider'?'topbar.changeProvider':state.mode==='update-daemon'?'topbar.updateDaemon':'topbar.install')},...(labels[state.phase]?[{label:labels[state.phase]}]:[])];
}
function topbarStatus() {
  const t=key=>locale.t(key,language),rows=state.inventory?.instances||[];
  const stopped=rows.find(r=>r.daemon&&(r.problem||(r.managed&&r.state==='stopped')));
  if(stopped)return `<button class="topbar-status topbar-status-problem" data-action="instances"><span class="health-dot" aria-hidden="true"></span>${escape(t('topbar.stopped').replace('{name}',stopped.daemon.name||''))}</button>`;
  if(update.state==='available')return `<button class="topbar-status" data-action="settings-updates"><span class="health-dot" aria-hidden="true"></span>${escape(t('topbar.update'))}</button>`;
  return '';
}
function topbar() {
  const t=key=>locale.t(key,language),list=crumbs().filter(c=>c.label);
  // Every step before the current page leads back to it.
  const left=list.map((c,i)=>i<list.length-1&&c.daemon?`<button class="crumb" data-daemon-manage="${escape(c.daemon)}" ${state.active?'disabled':''}>${escape(c.label)}</button>`:i<list.length-1&&c.action?`<button class="crumb" data-action="${c.action}">${escape(c.label)}</button>`:`<span class="crumb ${i===list.length-1?'crumb-current':''}" ${i===list.length-1?'aria-current="location"':''}>${escape(c.label)}</span>`).join('<span class="crumb-separator" aria-hidden="true">›</span>');
  const steps=started.steps,left_to_do=steps?steps.filter(s=>!s.done).length:null;
  const startedButton=steps&&left_to_do===0?'':`<button class="topbar-link" data-started-toggle aria-expanded="${started.open}">${escape(t('topbar.started'))}${left_to_do?` <span class="topbar-count">${left_to_do}</span>`:''}</button>`;
  const panel=started.open?`<div class="started-panel" role="dialog" aria-label="${escape(t('topbar.started'))}"><h2>${escape(t('topbar.started'))}</h2>${started.loading&&!steps?`<p class="hint">${escape(t('topbar.checking'))}</p>`:`<ol>${(steps||[]).map(s=>`<li class="${s.done?'done':''}"><span class="started-mark" aria-hidden="true">${s.done?'✓':''}</span><div>${s.done?`<b>${escape(t('started.'+s.key))}</b>`:`<button class="started-step" data-started-step="${s.key}">${escape(t('started.'+s.key))} →</button><p>${escape(t('started.'+s.key+'.how'))}</p>`}</div><span class="sr-only">${escape(t(s.done?'started.done':'started.todo'))}</span></li>`).join('')}</ol>`}</div>`:'';
  return `<header class="topbar"><nav class="crumbs" aria-label="${escape(t('topbar.location'))}">${left}</nav><div class="topbar-actions">${topbarStatus()}${startedButton}<button class="topbar-link" data-project-link="guide">${escape(t('topbar.help'))} ↗</button></div>${panel}</header>`;
}
// Each unfinished step leads to the place where it is done.
async function startedStep(key) {
  started.open=false;
  const reveal=selector=>requestAnimationFrame(()=>document.querySelector(selector)?.scrollIntoView({block:'start',behavior:'smooth'}));
  if(key==='install'){await action('new-instance');return;}
  if(key==='talk'){render();await window.companion.projectLink('telegram').catch(showError);return;}
  if(key==='backup'&&!google.connected){await action('settings');reveal('#settings-google');return;}
  if(!started.daemon){render();return;}
  await openDaemon(started.daemon);reveal(key==='obsidian'?'.obsidian-help':'.backup-help');
}
// Getting started: each step is read from what the app already knows.
async function refreshStarted() {
  if(started.loading)return;started.loading=true;render();
  try {
    const daemons=(state.inventory?.instances||[]).filter(i=>i.daemon),first=daemons[0];
    let talked=false,obsidian=false,daily=false;
    if(first){
      const [health,obs,backup]=await Promise.all([window.companion.health(first.id).catch(()=>null),window.companion.obsidianStatus(first.id).catch(()=>null),window.companion.backupStatus(first.id).catch(()=>null)]);
      talked=Boolean(health?.checks?.some(c=>c.key==='model'&&c.at&&['ok','check'].includes(c.status)));
      obsidian=Boolean(obs?.registered);daily=Boolean(backup?.automatic?.enabled);
    }
    started.daemon=first?.id||null;
    started.steps=[{key:'install',done:daemons.length>0},{key:'talk',done:talked},{key:'obsidian',done:obsidian},{key:'backup',done:daily}];
  } finally {started.loading=false;render();}
}
// What the user needs: one verdict and three plain answers, each with the one
// thing to do when something is wrong. The checks stay below for support.
const HEALTH_AREAS=[['reply',['host','plugin','model','conversation']],['memory',['memory','after-turn','worker','queue','search']],['night',['dreaming','rem']]];
function healthArea(r,name,keys) {
  const checks=Object.fromEntries(r.checks.filter(c=>keys.includes(c.key)).map(c=>[c.key,c])),bad=c=>c&&['action','issue'].includes(c.status);
  const row=state.inventory?.instances.find(i=>i.id===reportState.id),managed=Boolean(row?.managed),restartable=Boolean(row?.restartable);
  if(name==='reply'){
    if(checks.host?.diagnostic?.guidance==='start-host')return {state:'problem',text:'health.v2.reply.stopped',actions:['start']};
    if(bad(checks.host))return {state:'problem',text:'health.v2.reply.host',actions:['export']};
    // The host's own report of its Telegram connection (health.cjs).
    const channel=checks.conversation?.detail;
    if(channel==='channel-disconnected')return {state:'problem',text:'health.v2.reply.telegram',actions:restartable?['restart','export']:['export']};
    if(channel==='channel-token-rejected')return {state:'problem',text:'health.v2.reply.token',actions:['export']};
    if(channel==='channel-conflict')return {state:'problem',text:'health.v2.reply.conflict',actions:['export']};
    if(channel==='channel-connecting')return {state:'waiting',text:'health.v2.reply.connecting'};
    if(bad(checks.model))return {state:'problem',text:'health.v2.reply.model',actions:['telegram','export']};
    return {state:'ok',text:checks.model?.at?'health.v2.reply.ok':'health.v2.reply.new',at:checks.model?.at};
  }
  if(name==='memory'){
    const queue=checks.queue,events=queue?.diagnostic?.events||[],since=events.map(e=>e.at).filter(Boolean).sort()[0]||null;
    if(bad(checks.memory))return {state:'problem',text:'health.v2.memory.folder',actions:['export']};
    if(checks.worker&&checks.worker.status!=='ok'&&checks.worker.status!=='unknown')return {state:'problem',text:'health.v2.memory.worker',actions:managed?['restart','export']:['export']};
    if(bad(queue)||queue?.status==='check')return {state:'problem',text:'health.v2.memory.stuck',count:queue.count,at:since,actions:['export','help']};
    if([queue,checks['after-turn']].some(c=>c?.status==='recovering'))return {state:'waiting',text:'health.v2.memory.retrying'};
    if(bad(checks['after-turn']))return {state:'problem',text:'health.v2.memory.failed',at:checks['after-turn'].at,actions:['export','help']};
    const last=checks['after-turn']?.status==='ok'||checks['after-turn']?.status==='check'?checks['after-turn'].at:null;
    return {state:'ok',text:last?'health.v2.memory.ok':'health.v2.memory.new',at:last};
  }
  const failed=keys.map(k=>checks[k]).find(bad),last=keys.map(k=>checks[k]?.diagnostic?.lastSuccessAt).filter(Boolean).sort().at(-1)||null;
  if(failed)return {state:'notice',text:'health.v2.night.failed'};
  return {state:'ok',text:last?'health.v2.night.ok':'health.v2.night.new',at:last};
}
// The service behind a daemon's model, for its status page when an answer failed.
const MODEL_SERVICES={codex_cli:'openai',openai_api:'openai',anthropic_api:'anthropic',mistral_api:'mistral',xai_api:'xai'};
const modelService=id=>window.companion.providerStatus(id).then(p=>MODEL_SERVICES[p?.kind]||null,()=>null);
function healthSummary(r,tr,date) {
  const name=state.inventory?.instances.find(i=>i.id===reportState.id)?.daemon?.name||'';
  const areas=HEALTH_AREAS.map(([area,keys])=>({area,...healthArea(r,area,keys)}));
  const problem=areas.find(a=>a.state==='problem');
  const fill=(key,a)=>tr(key).replace('{name}',escape(name)).replace('{date}',a.at?`<span data-original>${date(a.at)}</span>`:'').replace('{count}',String(a.count||0));
  const button=(kind)=>kind==='start'?`<button class="button primary" data-health-start ${reportState.busy||state.active?'disabled':''}>${tr('health.start')}</button>`
    :kind==='restart'?`<button class="button primary" data-health-restart ${reportState.busy||state.active?'disabled':''}>${tr('health.v2.restart')}</button>`
    :kind==='telegram'?`<button class="button" data-project-link="telegram">${tr('health.telegram')}</button>`
    :kind==='help'?`<button class="text-button" data-project-link="contact">${tr('health.support')}</button>`
    :`<button class="text-button" data-health-export="" ${reportState.busy?'disabled':''}>${tr('health.v2.export')}</button>`;
  return `<div class="health-verdict health-verdict-${problem?'problem':'ok'}"><h2>${problem?fill(problem.text+'.title',problem):fill('health.v2.allGood',{})}</h2></div>
    <ul class="health-areas">${areas.map(a=>`<li class="health-area health-area-${a.state}" data-health-area="${a.area}"><span class="health-dot" aria-hidden="true"></span><div><h3>${tr('health.v2.'+a.area)}</h3><p>${fill(a.text,a)}${a.text==='health.v2.reply.model'?serviceLines([reportState.service]):''}</p>
      ${a.actions?.length?`<div class="links">${a.actions.map(button).join('')}</div>`:''}</div><span class="sr-only">${tr('health.v2.state.'+a.state)}</span></li>`).join('')}</ul>`;
}
function healthCard(c,tr,date,badge) {
  const d=c.diagnostic||{},events=d.events||[];
  const eventRows=events.map(e=>`<li><b>${tr('health.stage.'+e.stage)}</b> · <span data-original>${date(e.at)}</span>
    <p>${e.error?tr('health.error.'+healthErrorCategory(e.error)):''}</p><p>${e.error?`${tr('health.cause.recorded')} <code data-original>${escape(e.error)}</code>`:tr('health.cause.unknown')}</p>
    ${e.reason?`<p class="hint"><span>${tr('health.reason')}</span> <code data-original>${escape(e.reason)}</code></p>`:''}
    ${e.attempts!==undefined?`<p class="hint">${tr('health.attempts')} ${e.attempts}</p>`:''}</li>`).join('');
  return `<article class="health-check"><div class="row"><h2>${tr('health.check.'+c.key)}</h2>${badge(c.status)}</div><p>${tr('health.detail.'+c.detail)}</p>
    ${c.key==='queue'&&c.detail!=='queue-unavailable'?`<p class="small">${tr('health.queueCount')} ${c.count} · ${tr('health.queueFailed')} ${c.failed}</p>`:''}<p class="hint" data-original>${date(c.at)}</p>
    <details class="health-diagnostic" data-health-detail="${c.key}" ${reportState.expanded.has(c.key)?'open':''}><summary>${tr(['issue','action','recovering'].includes(c.status)?'health.problem':'health.details')}</summary>
      <h3>${tr('health.impact')}</h3><p>${tr('health.impact.'+c.key)}</p>
      ${d.lastSuccessAt?`<p>${tr('health.lastSuccess')} <span data-original>${date(d.lastSuccessAt)}</span></p>`:''}
      <h3>${tr('health.evidence')}</h3>${events.length?`<ul class="health-events">${eventRows}</ul>`:`<p>${tr('health.cause.unknown')}</p>`}
      ${d.limited?`<p>${tr('health.partial')}</p>`:''}
      <h3>${tr('health.next')}</h3><p>${tr('health.guidance.'+(d.guidance||'inspect'))}</p>
      ${d.recovery==='scheduled'||d.recovery==='running'?`<p>${tr('health.recovery.'+d.recovery)} ${d.nextAttemptAt?`<span data-original>${date(d.nextAttemptAt)}</span>`:''}</p>`:['issue','action','recovering'].includes(c.status)?`<p>${tr('health.recovery.unknown')}</p>`:''}
      <div class="links">${d.guidance==='start-host'?`<button class="button" data-health-start ${reportState.busy||state.active?'disabled':''}>${tr('health.start')}</button>`:''}
      ${c.key==='conversation'?`<button class="text-button" data-project-link="telegram">${tr('health.telegram')}</button>`:''}
      <button class="text-button" data-health-export="${c.key}" ${reportState.busy?'disabled':''}>${tr('health.exportOne')}</button><button class="text-button" data-project-link="contact">${tr('health.support')}</button></div>
    </details></article>`;
}

function healthErrorCategory(error) {
  if(['JSONDecodeError','ValidationError','ValueError','TypeError'].includes(error))return 'format';
  if(['AuthenticationError','PermissionDeniedError'].includes(error))return 'auth';
  if(['TimeoutError','TimeoutExpired','APITimeoutError'].includes(error))return 'timeout';
  if(['ConnectionError','ConnectionRefusedError','APIConnectionError'].includes(error))return 'connection';
  if(error==='FileNotFoundError')return 'missing-file';
  if(error==='PermissionError')return 'permission';
  if(error==='RateLimitError')return 'limit';
  return 'unknown';
}

function discardBody() {
  const p=discardState.plan;
  return `<h1>${p?.done?'Unfinished installation deleted':'Delete this unfinished installation?'}</h1>
    ${discardState.busy?'<div class="row busy" role="status"><span class="spinner"></span><span>Checking and cleaning up this installation…</span></div>':''}
    ${p&&!p.done?'<p>The private programs, downloads and background services created for this attempt will be removed. You will need to start a new installation to try again.</p><p>Other installations, your Telegram bot and provider accounts are not deleted.</p>':''}
    ${p?`<div class="note"><b>Your memory is kept.</b>${p.memory.length?p.memory.map(m=>`<p class="memory-path" data-original>${escape(m)}</p>`).join(''):'<p>No memory folder was created for this attempt.</p>'}</div>
    <details><summary>Retained files</summary><p>Diagnostics, settings, conversation history and saved local sign-ins stay in this folder. They can be removed manually after checking whether you need them.</p><p class="memory-path" data-original>${escape(p.retained)}</p></details>`:''}
    ${!p&&!discardState.busy?'<p>The installation has been kept. Return to the list to retry or open its diagnostic.</p>':''}`;
}
async function openDiscard(id) {
  if(state.active||state.instanceBusy||discardState.busy)return;
  discardState.id=id;discardState.plan=null;discardState.busy=true;state.page='discard';state.error='';render();
  try{discardState.plan=await window.companion.reviewDiscard(id);}
  catch(error){state.error=discardError(error);}
  finally{discardState.busy=false;render();}
}
function discardError(error) {
  const code=/discard\.([a-z]+)/.exec(error.message)?.[1]||'cleanup';
  try{return locale.t('discard.'+code,language);}catch{return locale.t('discard.cleanup',language);}
}
async function discardAction(action) {
  if(action==='discard-back'){state.page='instances';state.error='';await refreshInstances();return;}
  if(action!=='discard-confirm'||!discardState.plan)return;
  discardState.busy=true;state.error='';render();
  try {
    const p=discardState.plan;
    discardState.plan=await window.companion.applyDiscard(p.id,p.token);
    state.savedSetup=(await window.companion.preferences()).setup||'none';
    state.overview=null;state.setupStarted=false;
  }catch(error){state.error=discardError(error);discardState.plan=null;}
  finally{discardState.busy=false;render();}
}

async function startProviderSwitch(id) {
  if(state.active||state.instanceBusy||providerState.busy)return;
  hostAdditionState.id=id;
  Object.assign(state,{page:'setup',mode:'change-provider',operation:'setup',active:true,result:null,messages:[],guidance:[],links:[],prompt:null,phase:'model',error:'',modelCatalog:null,chatgptCode:'',chatgptStage:'',activity:'Preparing the model change…'});
  render();
  try{await window.companion.startProviderSwitch(id);}catch(error){state.active=false;showError(error);}
}

async function startHostAddition(id) {
  if(state.active||state.instanceBusy)return;
  hostAdditionState.id=id;
  Object.assign(state,{page:'setup',mode:'add-host',operation:'setup',active:true,result:null,messages:[],guidance:[],links:[],prompt:null,phase:'host',error:'',modelCatalog:null,chatgptCode:'',chatgptStage:'',activity:'Preparing to add OpenClaw…'});
  render();
  try{await window.companion.startHostAddition(id);}catch(error){state.active=false;showError(error);}
}

function modelCatalogBody(c) {
 const allowed=['gpt-6-astra','gpt-6.1-sol'];
 if(!c||!Array.isArray(c.models)||!c.models.every(m=>allowed.includes(m.id)&&['offered','absent-native'].includes(m.status)))return '';
 const tr=key=>escape(locale.t(key,language));
 const missing=c.models.filter(m=>m.status==='absent-native').map(m=>m.id);
 return '<details class="model-catalog"><summary>'+tr('chatgpt.catalog.title')+'</summary>'+
 (missing.length?'<p>'+tr('chatgpt.missing')+' <span data-original>'+escape(missing.join(', '))+'</span></p>':'')+'<p>'+tr('chatgpt.catalog.explanation')+'</p><ul>'+c.models.map(m=>'<li><span data-original>'+escape(m.id)+'</span>: '+tr('chatgpt.model.'+m.status)+'</li>').join('')+'</ul><p>'+escape(locale.t('chatgpt.catalog.counts',language,{native:c.native_count,excluded:c.excluded_count}))+'</p><p class="hint">'+tr('chatgpt.catalog.limit')+'</p></details>';
}

