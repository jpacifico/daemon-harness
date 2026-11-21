'use strict';
const { app, BrowserWindow, Menu, Tray, ipcMain, shell, dialog, nativeTheme, session } = require('electron');
// The window's session serves only the app's own files and cancels every other
// request. Google, Drive and update requests from this process use their own
// session, so that filter never applies to them.
let networkSession = null;
const networkFetch = (url, options) => (networkSession ??= session.fromPartition('companion-network')).fetch(url, options);
const path = require('node:path');
const fs = require('node:fs/promises');
const { pathToFileURL } = require('node:url');
const {Preferences} = require('./preferences.cjs');
const catalog = require('./locales/messages.json');
const {createLocalizer} = require('./i18n.js');
const locale = createLocalizer(catalog);
let preferences, instances, appInfo, obsidian, hostAddition, hostInterface, updates, updating = false;
const healthReports=new Map(), removalReports=new Map();
const about = require('./about.cjs');
const {SERVICES} = require('./services.js');
const background = () => nativeTheme.shouldUseDarkColors ? '#10141b' : '#f7f8fa';
// Windows draws its window buttons over the app's own top edge, in the main
// panel's colours, so the sidebar and the panel reach the top as on macOS.
const titleBarOverlay = () => ({color: nativeTheme.shouldUseDarkColors ? '#191e26' : '#ffffff', symbolColor: nativeTheme.shouldUseDarkColors ? '#eef2f7' : '#20232a', height: 44});
function applyFrameColors() {
  if(!window || window.isDestroyed())return;
  window.setBackgroundColor(background());
  if(process.platform === 'win32')window.setTitleBarOverlay(titleBarOverlay());
}
nativeTheme.on('updated', applyFrameColors);
const {Instances} = require('./instances.cjs');
const {SetupHomes} = require('./setup-homes.cjs');
const { Backend, allowedLink } = require('./backend.cjs');

const page = pathToFileURL(path.join(__dirname, 'index.html')).href;
const smoke = process.argv.includes('--smoke-test');
const smokeOpenedLinks = [];
// Each test build is signed anew, so macOS would ask for the login Keychain at
// every run. Tests never use a real credential: Chromium's mock keychain.
if (smoke) app.commandLine.appendSwitch('use-mock-keychain');
let window, backend, closing = false, tray = null, quitting = false;
// Started at login in the background: no window until the user opens it.
const startedHidden = process.argv.includes('--background') || (process.platform === 'darwin' && app.getLoginItemSettings().wasOpenedAtLogin);
app.setName('Daemons Companion');
app.setPath('userData', smoke
  ? require('node:fs').realpathSync(require('node:fs').mkdtempSync(path.join(require('node:os').tmpdir(), 'daemons-companion-ui-test-')))
  : path.join(app.getPath('appData'), 'Daemons Companion'));
if (!app.requestSingleInstanceLock()) app.quit();
app.on('second-instance', () => showWindow());
app.on('before-quit', () => { quitting = true; });
function showWindow() {
  if (!window || window.isDestroyed()) return;
  if (process.platform === 'darwin') app.dock?.show();
  if (window.isMinimized()) window.restore();
  window.show(); window.focus();
}
// The menu bar (Mac) or notification area (Windows) icon keeps the app reachable
// while it runs in the background; the login item starts it there.
function applyBackground() {
  if (smoke) return;
  const t = key => locale.t(key, preferences.language);
  if (preferences.background) {
    if (!tray) {
      tray = new Tray(path.join(__dirname, 'assets', process.platform === 'darwin' ? 'trayTemplate.png' : 'app-icon.ico'));
      tray.on('click', () => { if (process.platform !== 'darwin') showWindow(); });
    }
    tray.setToolTip('Daemons Companion');
    tray.setContextMenu(Menu.buildFromTemplate([
      {label: t('tray.open'), click: showWindow},
      {type: 'separator'},
      {label: t('tray.quit'), click: () => { quitting = true; app.quit(); }},
    ]));
  } else if (tray) { tray.destroy(); tray = null; }
  try {
    app.setLoginItemSettings({openAtLogin: preferences.background && preferences.openAtLogin, ...(process.platform === 'win32' ? {args: ['--background']} : {})});
  } catch { /* The login item is a convenience; the setting stays visible. */ }
}

function ownPage(event) {
  return window && event.sender === window.webContents &&
    event.senderFrame === window.webContents.mainFrame && event.senderFrame.url === page;
}
const mutations = new Set(['host-interface-open','instances-action','removal-review','removal-apply','daemon-save',
  'setup-start','setup-new','setup-discard-review','setup-discard-apply','host-add-start','update-install',
  'memory-backup','setup-restore','backup-drive','backup-automatic','google-disconnect','provider-change','provider-switch-start','host-change-start','daemon-update-start','daemon-restart']);
let mutationBusy = false;
function handle(channel, callback) {
  ipcMain.handle(channel, async (event, ...args) => {
    if (!ownPage(event)) throw new Error('Unsupported page.');
    if(!mutations.has(channel))return callback(...args);
    if(mutationBusy)throw new Error('attachment.busy');
    mutationBusy=true;
    try {return await callback(...args);} finally {mutationBusy=false;}
  });
}
handle('display-preferences', async () => ({language: preferences.language, theme: preferences.theme, background: preferences.background, openAtLogin: preferences.openAtLogin, platform: process.platform, about: appInfo, catalog, setup: await backend.setupHomes.summary(), update: updates.status()}));
handle('display-background', async (background, openAtLogin) => {
  await preferences.setBackground(background, openAtLogin); applyBackground();
  return {background: preferences.background, openAtLogin: preferences.openAtLogin};
});
handle('display-language', async language => {
  await preferences.set(language); updateMenu(); applyBackground(); return preferences.language;
});
handle('display-theme', async theme => {
  await preferences.setTheme(theme); nativeTheme.themeSource = preferences.theme;
  applyFrameColors();
  return preferences.theme;
});
handle('update-status', () => updates.status());
handle('update-check', () => updates.check({manual:true}));
handle('update-dismiss', () => updates.dismiss());
// Everything is downloaded and verified before the app closes; the helper then
// replaces it with the release's own install script and reopens it.
handle('update-install', async () => {
  if(backend.active||backend.settingsBusy||hostInterface?.busy)return {...updates.status(),error:'update.busy'};
  if(!await updates.prepare())return updates.status();
  if(backend.active||backend.settingsBusy||hostInterface?.busy){updates.abandon();return {...updates.status(),error:'update.busy'};}
  await hostInterface?.close().catch(()=>{});
  if(!await updates.start())return updates.status();
  updating=true;closing=true;
  setImmediate(()=>app.quit());
  return updates.status();
});
// Status pages of outside services, read only while a failure names one.
let serviceStatus = null;
const smokeStatusFetch = async () => new Response(JSON.stringify({page:{name:'Synthetic'},status:{indicator:'minor'},
  incidents:[{name:'Synthetic incident',status:'investigating',impact:'minor',components:[]}],components:[]}),{status:200});
handle('service-status', async id => (serviceStatus ??= new (require('./service-status.cjs').ServiceStatus)({fetch:smoke?smokeStatusFetch:networkFetch})).check(id));
handle('service-status-open', async id => {
  const page = typeof id === 'string' && Object.hasOwn(SERVICES, id) ? SERVICES[id].page : null;
  if (!page) throw new Error('Unsupported link.');
  if (smoke) smokeOpenedLinks.push(page); else await shell.openExternal(page);
});
handle('project-link', async key => {
  const url = about.destination(key);
  if(smoke)smokeOpenedLinks.push(url); else await shell.openExternal(url);
});
async function inventoryWithSetups(inventory = null) {
  const result=inventory||await instances.refresh();
  try {result.setups=await backend.setupHomes.list();}
  catch {result.setups=[];result.setupsWarning=true;}
  for(const setup of result.setups) {
    setup.discardSupported=require('./setup-discard.cjs').supported(process.platform);
    setup.instanceId=result.instances.find(r=>r.profile===setup.profile)?.id||null;
    delete setup.profile;
    try {readSetupDiagnostic(backend.setupHomes.roots.get(setup.id));setup.hasDiagnostic=true;}catch{setup.hasDiagnostic=false;}
  }
  return result;
}
async function setupDiagnostic(id) {
  if(id==null)return backend.diagnostics.read();
  const directory=await backend.setupHomes.resolve(id);
  return readSetupDiagnostic(directory);
}
function readSetupDiagnostic(directory) {
  if(backend.diagnosticHome===directory && backend.diagnostics.data)return backend.diagnostics.read();
  return new (require('./diagnostics.cjs').Diagnostics)(path.join(directory,'diagnostics')).read();
}
handle('instances-refresh', () => inventoryWithSetups());
// OpenClaw keeps its Telegram state in its running gateway: the daemon's own
// health query answers it (hosts/channel_status.py). Hermes writes it to a file.
async function openclawChannel(id) {
  if(smoke||backend.active||backend.settingsBusy)return null;
  const row=await ownedDaemon(id);
  if(!row||row.host!=='openclaw'||row.state!=='running')return null;
  try {const result=await backend.daemonSettings({operation:'channel-status',source_profile:row.profile,base:instances.base},'attachment');
    return ['connected','connecting','disconnected','token-rejected'].includes(result?.telegram)?{state:result.telegram,at:null}:null;}
  catch {return null;}
}
// OpenClaw's last answer per daemon, from its health page or a check after a
// restart; a restart forgets it, since it described the previous gateway.
const openclawChannels=new Map();
// "Restart the daemon" when its host is not connected to Telegram: the same
// gateway restart the app does after a model change, without a confirmation.
handle('daemon-restart', async id => {
  if(backend.active||backend.settingsBusy)throw new Error('Finish or stop setup before managing another instance.');
  openclawChannels.delete(id);
  try {return await instances.restartGateway(id);}catch{throw new Error('daemons.restart-failed');}
});
// Each daemon card shows its Telegram state without opening its health page:
// Hermes from its state file (instant), OpenClaw from its last answer. The ids
// in `live` (OpenClaw daemons just restarted) are asked again; the query holds
// the app's settings commands, so the page asks rarely (renderer.js).
handle('daemon-channels', async (live=[]) => {
  const fresh=new Set(Array.isArray(live)?live.filter(x=>typeof x==='string').slice(0,16):[]);
  const result={};
  for(const row of instances.rows.values()) {
    if(!row.daemon)continue;
    try {
      if(row.host==='hermes'){const c=await require('./health.cjs').hermesChannel(row);if(c)result[row.id]=c.state;continue;}
      if(fresh.has(row.id)){const c=await openclawChannel(row.id);if(c)openclawChannels.set(row.id,c.state);}
      const state=openclawChannels.get(row.id);
      if(['connected','connecting','disconnected','token-rejected','conflict'].includes(state))result[row.id]=state;
    } catch {/* No answer: the card shows nothing. */}
  }
  return result;
});
handle('daemon-health', async id => {
  const channel=await openclawChannel(id);
  if(channel)openclawChannels.set(id,channel.state);else openclawChannels.delete(id);
  const report=await instances.health(id,{channel});if(healthReports.size>=128)healthReports.clear();healthReports.set(id,report);return report;
});
handle('setup-diagnostic', id => setupDiagnostic(id));
let discardReview=null;
async function discardContext(id) {
  return require('./setup-discard.cjs').context({platform:process.platform,backend,instances},id);
}
handle('setup-discard-review', async id => {
  discardReview=null;
  const context=await discardContext(id);
  const plan=await backend.daemonSettings({...context,action:'review'},'discard');
  const token=require('node:crypto').randomUUID();
  discardReview={id,token,context,revision:plan.revision,at:Date.now()};
  return {...plan,id,token};
});
handle('setup-discard-apply', async (id,token) => {
  const review=discardReview;
  if(!review||review.id!==id||review.token!==token||Date.now()-review.at>600000)throw new Error('discard.changed');
  const context=await discardContext(id);
  if(JSON.stringify(context)!==JSON.stringify(review.context))throw new Error('discard.changed');
  discardReview=null;
  const diagnostic=new (require('./diagnostics.cjs').Diagnostics)(path.join(context.root,'diagnostics'),backend.metadata);
  diagnostic.begin();diagnostic.record({phase:'installation',operation:'discard',status:'started'});
  backend.diagnosticHome=context.root;backend.diagnostics=diagnostic;
  try {
    const result=await backend.daemonSettings({...context,action:'apply',revision:review.revision},'discard');
    diagnostic.record({phase:'installation',operation:'discard',status:'completed'});
    diagnostic.observe({type:'finished',result:'completed'});
    backend.overview=null;
    return {...result,id};
  }catch(error) {
    const code=/discard\.([a-z]+)/.exec(error.message)?.[1]||'cleanup';
    diagnostic.record({phase:'installation',operation:'discard',status:'failed',error_code:'discard.'+code});
    diagnostic.observe({type:'finished',result:'failed'});
    throw error;
  }
});
// Local date and time in exported file names, so successive reports stay distinct.
function exportStamp(now=new Date()) {
  const two=value=>String(value).padStart(2,'0');
  return `${now.getFullYear()}-${two(now.getMonth()+1)}-${two(now.getDate())}-${two(now.getHours())}${two(now.getMinutes())}`;
}
handle('export-diagnostic', async (kind,id,key=null) => {
  let report=kind==='update'?await updates.diagnostic():kind==='removal'?removalReports.get(id):kind==='setup'?await setupDiagnostic(id):kind==='health'?healthReports.get(id):kind==='host'?backend.hostDiagnostic((await hostAdditionController().context(id)).source.profile).read():null;
  if(!report)throw new Error('Open the report before exporting it.');
  if(kind==='health'&&key!==null)report=require('./health.cjs').focusedReport(report,key);
  const content=JSON.stringify(report,null,2)+'\n';
  if(smoke)return {exported:true,report};
  const result=await dialog.showSaveDialog(window,{title:locale.t('diagnostic.export',preferences.language),defaultPath:'daemons-'+kind+(key&&kind==='health'?'-'+report.checks[0].key:'')+'-diagnostic-'+exportStamp()+'.json',filters:[{name:'JSON',extensions:['json']}]});
  if(result.canceled||!result.filePath)return {exported:false};
  // Replace the chosen file itself atomically; do not follow a symlink target.
  const temporary=result.filePath+'.'+require('node:crypto').randomUUID()+'.tmp';
  try {await fs.writeFile(temporary,content,{mode:0o600,flag:'wx'});await fs.rename(temporary,result.filePath);}
  finally {await fs.unlink(temporary).catch(()=>{});}
  return {exported:true};
});
handle('instances-add', async () => {
  const result = await dialog.showOpenDialog(window, {properties:['openDirectory'], title:locale.t('instances.choose',preferences.language)});
  return inventoryWithSetups(result.canceled ? null : await instances.add(result.filePaths[0]));
});
handle('instances-action', async (id, action) => {
  if(backend.active||backend.settingsBusy)throw new Error('Finish or stop setup before managing another instance.');
  if(['start','stop','restart'].includes(action))openclawChannels.delete(id);
  return instances.change(id,action,async row => {
    const answer = await dialog.showMessageBox(window,{type:'question',
      message:locale.t('instances.confirm.'+action,preferences.language),
      detail:row.host+'\n'+row.profile+'\n\n'+locale.t('instances.gateway.scope',preferences.language),
      buttons:[locale.t('instances.cancel',preferences.language),locale.t('instances.confirm',preferences.language)],defaultId:0,cancelId:0});
    if(answer.response===1&&['stop','restart'].includes(action))await hostInterface?.closeFor(id);
    return answer.response===1;
  });
});
function blockedRemoval(id,phase,error) {
  instances.removalReview=null;
  const result=require('./removal-diagnostics.cjs').blocked(error,phase,backend.metadata);
  if(removalReports.size>=32)removalReports.clear();
  removalReports.set(id,result.diagnostic);
  return result;
}
handle('removal-review', async (id,action) => {
  if(backend.active||backend.settingsBusy)throw new Error('Finish or stop setup before managing another instance.');
  removalReports.delete(id);
  try {return await instances.reviewRemoval(id,action,request=>backend.daemonSettings(request,true));}
  catch(error){return blockedRemoval(id,'review',error);}
});
handle('removal-apply', async (id,token,name,deleteMemory=false) => {
  await hostInterface?.closeFor(id);
  if(backend.active||backend.settingsBusy)throw new Error('Finish or stop setup before managing another instance.');
  const review=instances.removalReview,t=(key,args)=>locale.t(key,preferences.language,args);
  if(deleteMemory===true) {
    if(!review||review.id!==id||review.token!==token||!review.plan.memoryDeletable)throw new Error('removal.changed');
    // The second confirmation, before anything changes; cancelling keeps everything.
    const answer=await dialog.showMessageBox(window,{type:'warning',
      message:t('removal.memory.confirm',{name:review.plan.name}),
      detail:t('removal.memory.confirm.detail',{files:review.plan.memoryFiles,size:Math.max(1,Math.round(review.plan.memoryBytes/1e6))})+'\n\n'+review.plan.memory,
      buttons:[t('instances.cancel'),t('removal.memory.confirm.button')],defaultId:0,cancelId:0});
    if(answer.response!==1)return {cancelled:true};
  }
  removalReports.delete(id);
  let result;
  try {
    result=await instances.applyRemoval(id,token,name,request=>backend.daemonSettings(request,true));
    backend.overview=null;
  }catch(error){return blockedRemoval(id,'apply',error);}
  if(deleteMemory!==true||!result.done)return result;
  // The uninstall stands either way; a refused or failed move keeps the memory.
  try {
    const target=await instances.memoryRemoval(id,token,request=>backend.daemonSettings(request,true));
    await (smoke&&backend.smokeTrash?backend.smokeTrash:shell.trashItem)(target);
    if(await fs.stat(target).then(()=>true,()=>false))throw new Error('removal.unsafe');
    return {...result,memoryRemoved:true};
  }catch{return {...result,memoryRemoved:false};}
});
handle('removal-folder', async (id,token,kind,copy) => {
  const target=await instances.removalTarget(id,token,kind);
  if(smoke)return target;
  if(copy===true)require('electron').clipboard.writeText(target);
  else if(await shell.openPath(target))throw new Error('This folder could not be opened.');
});
function hostAdditionController() {
  if(backend.active||backend.settingsBusy)throw new Error('attachment.busy');
  if(!hostAddition)hostAddition=new (require('./host-addition.cjs').HostAddition)(instances);
  return hostAddition;
}
// The guided session asks for every confirmation itself, one screen at a time.
handle('host-add-start', async id => {
  const request=await hostAdditionController().request(id);
  backend.conversationLanguage=preferences.language;
  return backend.start('add-host',null,request);
});
handle('daemon-settings', async id => {
  if(backend.active||backend.settingsBusy)throw new Error('Finish or stop setup before managing another instance.');
  return instances.inspectDaemon(id,request=>backend.daemonSettings(request));
});
handle('daemon-save', async (id,token,values) => {
  if(backend.active||backend.settingsBusy)throw new Error('Finish or stop setup before managing another instance.');
  return instances.saveDaemon(id,token,values,request=>backend.daemonSettings(request),async context=>{
    const answer=await dialog.showMessageBox(window,{type:'question',
      message:locale.t('daemon.shared.confirm',preferences.language),
      detail:locale.t('daemon.shared.detail',preferences.language)+'\n\n'+[context.row,...context.shared].map(r=>r.host+' — '+r.profile).join('\n'),
      buttons:[locale.t('instances.cancel',preferences.language),locale.t('daemon.save',preferences.language)],defaultId:0,cancelId:0});
    return answer.response===1;
  });
});
handle('daemon-memory-copy', async id => {
  const target=await instances.target(id,'memory');
  if(smoke)return target;
  require('electron').clipboard.writeText(target);
});
handle('instances-open', async (id,kind) => {
  const target=await instances.target(id,kind);
  if(smoke)return target;
  if(await shell.openPath(target))throw new Error('This folder could not be opened.');
});
handle('host-interface-open',async id=>{
  if(smoke&&backend.smokeOpenHostInterface)return backend.smokeOpenHostInterface(id);
  if(backend.active||backend.settingsBusy)throw new Error('hostInterface.busy');
  if(!hostInterface)hostInterface=new (require('./host-interface.cjs').HostInterface)({instances,
    run:request=>backend.daemonSettings(request,'interface'),
    open:url=>smoke?smokeOpenedLinks.push(url):shell.openExternal(url),
    assets:path.join(backend.packageRoot,'../host-interfaces/hermes'),
    stateRoot:path.join(app.getPath('userData'),'host-interfaces')});
  try{return await hostInterface.launch(id);}catch(e){const key=String(e.message||'');throw new Error(key.startsWith('hostInterface.')?key:'hostInterface.unavailable');}
});
handle('obsidian-status', async id => {
  const info=await obsidian.inspect(await instances.target(id,'memory'));
  return {available:info.available,registered:info.registered};
});
handle('obsidian-open', async id => obsidian.launch(await instances.target(id,'memory')));
// Moving a daemon to its other host (hosts/change.py): the same ownership checks as
// an uninstall, then a guided setup that asks each confirmation itself.
handle('host-change-start', async id => {
  if(backend.active||backend.settingsBusy)throw new Error('change.busy');
  let row;
  try{({row}=await instances.removalContext(id));}catch{throw new Error('change.not-owned');}
  const request={profile:row.profile,base:instances.base};
  await backend.daemonSettings({action:'inspect',...request},'change');
  backend.conversationLanguage=preferences.language;
  return backend.start('new',null,null,null,false,request);
});
// Model and API key of a daemon the app created (desktop/provider_settings.py).
// A change is committed only after its gateway restarted; otherwise it is reverted.
// The smoke test replaces both with synthetic ones: no credential store, network or service.
const providerRun=request=>smoke&&backend.smokeProvider?backend.smokeProvider.run(request):backend.daemonSettings(request,'provider');
const providerRestart=id=>smoke&&backend.smokeProvider?backend.smokeProvider.restart(id):instances.restartGateway(id);
async function providerProfile(id) {
  if(backend.active||backend.settingsBusy)throw new Error('provider.busy');
  await instances.refresh();const row=instances.rows.get(id);
  // Python checks that the profile belongs to an installation the app created.
  if(!row?.profile||row.problem)throw new Error('provider.unsupported');
  return row.profile;
}
async function providerRevert(id,profile) {
  // After a change of provider the services captured before are restarted too.
  await providerRun({action:'revert',profile,base:instances.base});
  await providerRestart(id).catch(()=>{});
}
handle('provider-status', async id => {
  const profile=await providerProfile(id);
  const info=await providerRun({action:'describe',profile});
  if(!info.pending)return info;
  // An interrupted change (app closed or crashed) returns to the previous settings.
  await providerRevert(id,profile);
  return {...await providerRun({action:'describe',profile}),recovered:true};
});
handle('provider-check', async (id,key=null) => providerRun({action:'check',profile:await providerProfile(id),key}));
handle('provider-change', async (id,revision,model,key=null) => {
  const profile=await providerProfile(id);
  const result=await providerRun({action:'change',profile,revision,model,key});
  if(result.status!=='applied')return result;
  await hostInterface?.closeFor(id);
  let restarted;
  try {restarted=await providerRestart(id);}
  catch {await providerRevert(id,profile);throw new Error('provider.restart-failed');}
  await providerRun({action:'commit',profile});
  return {status:'committed',restarted};
});
// Another provider for the same daemon: a guided session (hosts/provider_change.py),
// because ChatGPT needs its native sign-ins and a reply confirms the change.
handle('provider-switch-start', async id => {
  const profile=await providerProfile(id);
  const name=instances.rows.get(id)?.daemon?.name||'';
  const info=await providerRun({action:'describe',profile});
  if(!info.switchable||info.pending)throw new Error('provider.unsupported');
  backend.conversationLanguage=preferences.language;
  return backend.start('change-provider',null,null,null,false,null,{profile,base:instances.base,name:name||'Your daemon'});
});
// Runtime update of a daemon this app created (hosts/update.py, runtime/upgrade.py).
// The page asks what is possible; the update itself is a guided session.
async function ownedDaemon(id) {
  await instances.refresh();const row=instances.rows.get(id);
  const owned=row?.profile&&!row.problem&&row.daemon?.runtime&&(row.profile===instances.base||row.profile.startsWith(instances.base+path.sep));
  return owned?row:null;
}
handle('daemon-update-status', async id => {
  if(smoke||backend.active||backend.settingsBusy)return null;
  const row=await ownedDaemon(id);
  if(!row)return null;
  try {return await backend.daemonSettings({operation:'upgrade-check',source_profile:row.profile,base:instances.base},'attachment');}
  catch {return {status:'unavailable'};}
});
handle('daemon-update-start', async id => {
  if(backend.active||backend.settingsBusy)throw new Error('Finish the current setup or daemon action, then try again.');
  const row=await ownedDaemon(id);
  if(!row)throw new Error('This daemon cannot be updated by the app.');
  backend.conversationLanguage=preferences.language;
  await hostInterface?.closeFor(id);
  return backend.start('update-daemon',null,null,null,false,null,null,{source_profile:row.profile,base:instances.base,name:row.daemon?.name||'Your daemon'});
});
// Memory backups (backup-service.cjs): to a file or Google Drive, by hand or daily.
const {Backups,checkedPassword,fileName,EXTENSION:BACKUP_EXTENSION}=require('./backup-service.cjs');
const backupCrypto=require('./backup-crypto.cjs');
let backups=null,restoreChoice=null,restoreDownloaded=null;
function backupsFor() {
  if(!backups) {
    let oauthConfig=null;
    // Packaged builds carry the publisher's Google client; source runs may name one.
    for(const file of [path.join(__dirname,'google-oauth.json'),!app.isPackaged?process.env.DAEMONS_GOOGLE_OAUTH_CONFIG:'']) {
      try {if(file){const value=JSON.parse(require('node:fs').readFileSync(file,'utf8'));oauthConfig=value.installed||value;break;}}catch{/* Drive stays unavailable. */}
    }
    backups=new Backups({userData:app.getPath('userData'),instances,backend,safeStorage:require('electron').safeStorage,oauthConfig,
      open:url=>smoke?smokeOpenedLinks.push(url):shell.openExternal(url),fetchImpl:networkFetch});
  }
  return backups;
}
async function privateFolder(prefix) { return backupsFor().folder(prefix); }
handle('memory-backup', async (id,password) => {
  checkedPassword(password);
  if(backend.active||backend.settingsBusy)throw new Error('backup.busy');
  const {row}=await instances.daemonContext(id);
  let destination;
  if(smoke)destination=path.join(await privateFolder('smoke-'),fileName(row.daemon.name)+'.'+BACKUP_EXTENSION);
  else {
    const chosen=await dialog.showSaveDialog(window,{title:locale.t('backup.save',preferences.language),
      defaultPath:fileName(row.daemon.name)+'-memory-'+exportStamp()+'.'+BACKUP_EXTENSION,filters:[{name:'Daemons backup',extensions:[BACKUP_EXTENSION]}]});
    if(chosen.canceled||!chosen.filePath)return {saved:false};
    destination=path.join(await fs.realpath(path.dirname(chosen.filePath)),path.basename(chosen.filePath));
  }
  const result=await backupsFor().toFile(id,password,destination);
  return smoke?{...result,path:destination}:result;
});
handle('backup-drive', async (id,password) => {
  checkedPassword(password);
  if(backend.active||backend.settingsBusy)throw new Error('backup.busy');
  return backupsFor().toDrive(id,{password});
});
handle('backup-status', id => backupsFor().status(id??null));
handle('google-connect', () => backupsFor().connect());
handle('google-disconnect', () => backupsFor().disconnect());
handle('backup-automatic', async (id,enabled,password=null) => {
  if(!enabled)return backupsFor().disableAutomatic(id);
  const status=await backupsFor().enableAutomatic(id,password);
  const background=!preferences.background;
  if(background){await preferences.setBackground(true,true);applyBackground();}
  // The first daily backup starts now rather than at the next hourly check.
  setImmediate(()=>backupsFor().runDue().catch(()=>{}));
  return {...status,backgroundEnabled:background};
});
handle('drive-backups', () => backupsFor().list());
handle('backup-choose', async (smokePath=null,driveId=null) => {
  restoreChoice=null;
  if(restoreDownloaded){await fs.rm(path.dirname(restoreDownloaded),{recursive:true,force:true});restoreDownloaded=null;}
  if(smoke&&typeof smokePath==='string')restoreChoice=smokePath;
  else if(typeof driveId==='string') {
    // A Drive backup is downloaded and checked before its password is asked.
    restoreChoice=restoreDownloaded=await backupsFor().download(driveId);
    return {chosen:true,file:'Google Drive',drive:true};
  } else {
    const chosen=await dialog.showOpenDialog(window,{title:locale.t('backup.choose',preferences.language),properties:['openFile'],
      filters:[{name:'Daemons backup',extensions:[BACKUP_EXTENSION]}]});
    if(chosen.canceled||!chosen.filePaths[0])return {chosen:false};
    restoreChoice=chosen.filePaths[0];
  }
  return {chosen:true,file:path.basename(restoreChoice)};
});
handle('setup-restore', async (password,move=false) => {
  checkedPassword(password);
  if(!restoreChoice)throw new Error('backup.chooseFirst');
  if(backend.active||backend.settingsBusy)throw new Error('backup.busy');
  const work=await privateFolder('restore-'),archive=path.join(work,'backup.zip');
  try {
    const source=await fs.realpath(restoreChoice);
    await backupCrypto.decrypt(source,archive,password);
    const identity=await backend.daemonSettings({action:'restore-inspect',archive},'backup');
    backend.conversationLanguage=identity.language||preferences.language;
    await backend.start('new',null,null,archive,move===true);
    restoreChoice=null;
    return {started:true,name:identity.name};
  } catch(error) {
    throw new Error(String(error.message||'').startsWith('backup.')||String(error.message||'').startsWith('archive.')||String(error.message||'').startsWith('restore.')?error.message:'backup.unavailable');
  } finally {
    // The setup moved the archive into its own folder; anything left is discarded.
    await fs.rm(work,{recursive:true,force:true});
    if(restoreChoice===null&&restoreDownloaded){await fs.rm(path.dirname(restoreDownloaded),{recursive:true,force:true});restoreDownloaded=null;}
  }
});
handle('setup-start', id => {backend.conversationLanguage=preferences.language;return backend.start('setup',id);});
handle('setup-new', () => {backend.conversationLanguage=preferences.language;return backend.start('new');});
handle('setup-answer', answer => backend.answer(answer));
handle('setup-cancel', () => backend.cancel());
handle('setup-status', () => backend.start('status'));
handle('open-memory', async () => {
  const vault = backend.overview?.vault;
  const relative = typeof vault === 'string' ? path.relative(backend.installHome, vault) : '';
  if (!backend.overview?.installed || !relative || relative.startsWith('..') || path.isAbsolute(relative) ||
      path.resolve(await fs.realpath(vault)) !== path.resolve(vault)) throw new Error('Refresh your private installation status first.');
  const error = await shell.openPath(vault);
  if (error) throw new Error('The memory folder could not be opened.');
});
handle('open-link', async url => {
  if (!allowedLink(url) || !(url === 'https://web.telegram.org/' || backend.links.has(url))) throw new Error('Unsupported link.');
  if (smoke) smokeOpenedLinks.push(url);
  else await shell.openExternal(url);
});

// GitHub CLI is the fallback for releases the public cannot read. The first one that answers is kept.
let ghPath = null;
async function findGh() {
  const {execFile} = require('node:child_process');
  const run = (file, args, timeout) => new Promise((resolve, reject) => execFile(file, args, {timeout, windowsHide:true, maxBuffer:1024*1024},
    (error, stdout) => error ? reject(error) : resolve(stdout)));
  if(!ghPath) for(const candidate of require('./updates.cjs').ghCandidates(process.platform, process.env)) {
    try {await run(candidate, ['--version'], 10000); ghPath=candidate; break;} catch { /* next */ }
  }
  return ghPath ? (args, timeout) => run(ghPath, args, timeout) : null;
}
function updateMenu() {
  const t = key => locale.t(key, preferences.language);
  const item = role => ({role, label: t('menu.' + role)});
  const aboutItem = {label:t('menu.about'), click:()=>window?.webContents.send('setup-event',{type:'navigate-about'})};
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    ...(process.platform === 'darwin' ? [{label: 'Daemons', submenu: [aboutItem, item('quit')]}] : []),
    {label: t('menu.edit'), submenu: [item('undo'), item('redo'), {type: 'separator'}, item('cut'), item('copy'), item('paste'), item('selectAll')]},
    {label: t('menu.window'), submenu: [item('minimize'), item('close')]},
    ...(process.platform === 'darwin' ? [] : [{label:t('menu.help'), submenu:[aboutItem]}])
  ]));
}

app.whenReady().then(async () => {
  const packageRoot = app.isPackaged ? path.join(process.resourcesPath, 'installer') : path.join(__dirname, 'runtime');
  let metadata = {};
  try { metadata = JSON.parse(await fs.readFile(path.join(__dirname, 'runtime.json'), 'utf8')); } catch { /* Start reports missing package. */ }
  // A source run can be isolated from the installed app's state for a canary;
  // packaged builds ignore this variable.
  const devBase = !app.isPackaged && path.isAbsolute(process.env.DAEMONS_COMPANION_DEV_BASE || '') ? process.env.DAEMONS_COMPANION_DEV_BASE : '';
  const base = smoke ? path.join(app.getPath('userData'),'DaemonHarness') : devBase ? path.join(devBase, 'DaemonHarness') : process.platform === 'win32' ? path.join(process.env.LOCALAPPDATA || path.join(app.getPath('home'), 'AppData', 'Local'), 'DaemonHarness') : path.join(app.getPath('appData'), 'DaemonHarness');
  const installHome = path.join(base, 'Companion', 'private');
  backend = new Backend({ packageRoot, metadata, installHome,
    setupHomes: new SetupHomes(path.join(base,'Companion')),
    bootstrapHome: path.join(base, 'Companion', 'bootstrap'),
    emit: data => { if (window && !window.isDestroyed()) window.webContents.send('setup-event', data); } });
  obsidian=new (require('./obsidian.cjs').Obsidian)({appData:smoke?app.getPath('userData'):app.getPath('appData'),
    application:uri=>smoke?Promise.resolve({path:'/synthetic/Obsidian'}):app.getApplicationInfoForProtocol(uri),
    running:smoke?async()=>Boolean(backend.smokeObsidianRunning):undefined,
    open:uri=>smoke?smokeOpenedLinks.push(uri):shell.openExternal(uri)});
  if (smoke) backend.smokeOpenedLinks = smokeOpenedLinks;
  // The packaged smoke test replaces Google with a synthetic Drive; never a real account.
  if (smoke) backend.smokeBackups = value => { backups = value; };
  if (smoke) backend.networkFetch = networkFetch;
  preferences = new Preferences(app.getPath('userData'));
  await preferences.load();
  nativeTheme.themeSource = preferences.theme;
  appInfo = about.information({version:app.getVersion(), revision:metadata.revision});
  updates = new (require('./updates.cjs').Updates)({channel:metadata.channel, revision:metadata.revision,
    platform:process.platform, arch:process.arch, execPath:process.execPath, pid:process.pid, env:process.env,
    root:path.join(app.getPath('userData'),'updates'), helpers:path.join(__dirname,'updater'),
    packaged:app.isPackaged&&!smoke&&!devBase, fetch:networkFetch, gh:findGh,
    writable:directory=>{try{require('node:fs').accessSync(directory,require('node:fs').constants.W_OK);return true;}catch{return false;}},
    // Resolves with the exit code of an attached helper; a detached one is left running.
    launch:(command,args,{detached})=>new Promise(resolve=>{const child=require('node:child_process').spawn(command,args,{detached,stdio:'ignore',windowsHide:true});
      if(detached){child.unref();resolve(null);return;}child.once('error',()=>resolve(null));child.once('exit',code=>resolve(code));}),
    emit:status=>{if(window&&!window.isDestroyed())window.webContents.send('setup-event',{type:'update',status});}});
  await updates.startup();
  instances = new Instances({home:smoke?path.join(app.getPath('userData'),'home'):app.getPath('home'),appData:app.getPath('userData'),base});
  await instances.load();
  updateMenu();
  window = new BrowserWindow({ width: 1040, height: 780, minWidth: 860, minHeight: 680,
    show: false, title: 'Daemons Companion', backgroundColor: background(),
    icon: path.join(__dirname, 'assets', 'app-icon.png'),
    ...(process.platform === 'darwin' ? { titleBarStyle: 'hiddenInset', trafficLightPosition: { x: 20, y: 22 } } : {}),
    ...(process.platform === 'win32' ? { titleBarStyle: 'hidden', titleBarOverlay: titleBarOverlay() } : {}),
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true, nodeIntegration: false, sandbox: true, webSecurity: true,
      backgroundThrottling: !smoke }
  });
  window.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  window.webContents.session.setPermissionCheckHandler(() => false);
  // Every file the window loads (window-files.test.cjs checks index.html against it).
  const allowed = new Set(['index.html', 'theme.css', 'renderer.js', 'daemon-cards.js', 'i18n.js', 'services.js', 'assets/daemons-dervaux.svg'].map(name => pathToFileURL(path.join(__dirname, name)).href));
  window.webContents.session.webRequest.onBeforeRequest((details, callback) => callback({ cancel: !allowed.has(details.url) }));
  window.webContents.on('will-navigate', event => event.preventDefault());
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.on('close', async event => {
    if(updating)return;
    // In the background, closing hides the window; quitting comes from the icon or Cmd+Q.
    if(preferences.background && !quitting && !smoke){event.preventDefault();window.hide();if(process.platform==='darwin')app.dock?.hide();return;}
    if(hostInterface?.busy){event.preventDefault();return;}
    if(hostInterface?.sessions.size&&!closing){event.preventDefault();await hostInterface.close();if(!window.isDestroyed())window.close();return;}
    if(backend.settingsBusy) { backend.authChild?.kill(); event.preventDefault(); await backend.settingsStopped; if(!window.isDestroyed())window.close(); return; }
    if (!backend.active || closing) return;
    event.preventDefault();
    const { response } = await dialog.showMessageBox(window, { type: 'question',
      message: locale.t('close.title', preferences.language), detail: locale.t('close.detail', preferences.language),
      buttons: [locale.t('close.keep', preferences.language), locale.t('button.stop', preferences.language)], defaultId: 0, cancelId: 0 });
    if (response !== 1) return;
    backend.cancel();
    await backend.stopped;
    closing = true;
    window.close();
  });
  await window.loadURL(page);
  applyBackground();
  if (startedHidden && preferences.background && !smoke) { if (process.platform === 'darwin') app.dock?.hide(); }
  else window.show();
  updates.schedule();
  if(!smoke){setTimeout(()=>backupsFor().runDue().catch(()=>{}),60000).unref?.();setInterval(()=>backupsFor().runDue().catch(()=>{}),3600000).unref?.();}
  if (smoke) {
    try { console.log(JSON.stringify(await (process.argv.includes('--removal-native-smoke') ? require('./smoke.cjs').runRemoval : process.argv.includes('--discard-native-smoke') ? require('./smoke.cjs').runDiscard : require(process.argv.includes('--attachment-native-smoke') ? './attachment-native-smoke.cjs' : './smoke.cjs').run)(window, backend, instances))); }
    catch (error) { console.error(error.message); app.exit(1); return; }
    app.quit();
  }
});
app.on('window-all-closed', () => app.quit());
