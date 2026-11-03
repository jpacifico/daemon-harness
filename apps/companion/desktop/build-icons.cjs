'use strict';
// Build-only entry: isolated Electron profile, no Companion backend or services.
const {app, BrowserWindow} = require('electron');
const fs = require('node:fs/promises');
const syncFs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const scratch = syncFs.mkdtempSync(path.join(os.tmpdir(), 'daemons-icon-build-'));
app.setPath('userData', path.join(scratch, 'profile'));
app.commandLine.appendSwitch('force-device-scale-factor', '1');
app.whenReady().then(async () => {
  if (process.platform !== 'darwin') throw new Error('Generate the full icon set on macOS (iconutil required).');
  const assets = path.join(__dirname, 'assets');
  const svg = await fs.readFile(path.join(assets, 'app-icon.svg'), 'utf8');
  const window = new BrowserWindow({width:1024,height:1024,useContentSize:true,
    show:false,transparent:true,frame:false,enableLargerThanScreen:true,webPreferences:{sandbox:true,contextIsolation:true,nodeIntegration:false}});
  await window.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent('<style>html,body{margin:0;width:1024px;height:1024px;background:transparent}svg{display:block}</style>' + svg));
  await window.webContents.executeJavaScript('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))');
  const capture = await window.webContents.capturePage();
  if (capture.getSize().width !== capture.getSize().height) throw new Error("Non-square icon render");
  const master = capture.resize({width:1024,height:1024,quality:"best"});
  if (master.getSize().width !== 1024 || master.getSize().height !== 1024) throw new Error('Unexpected render dimensions: ' + JSON.stringify(master.getSize()));
  await fs.writeFile(path.join(assets, 'app-icon.png'), master.toPNG());
  const iconset = path.join(scratch, 'AppIcon.iconset');
  await fs.mkdir(iconset);
  for (const logical of [16,32,128,256,512]) {
    for (const scale of [1,2]) {
      const size = logical * scale;
      await fs.writeFile(path.join(iconset, `icon_${logical}x${logical}${scale===2?'@2x':''}.png`),
        master.resize({width:size,height:size,quality:'best'}).toPNG());
    }
  }
  execFileSync('/usr/bin/iconutil', ['-c','icns',iconset,'-o',path.join(assets,'app-icon.icns')]);
  // ICO entries hold PNG images, supported by Windows Vista and later.
  const sizes = [16,24,32,48,64,128,256];
  const entries = sizes.map(size => master.resize({width:size,height:size,quality:'best'}).toPNG());
  const header = Buffer.alloc(6 + sizes.length * 16);
  header.writeUInt16LE(1,2); header.writeUInt16LE(sizes.length,4);
  let offset = header.length;
  sizes.forEach((size,i) => {
    const at = 6+i*16;
    header[at] = size===256?0:size; header[at+1] = size===256?0:size;
    header.writeUInt16LE(1,at+4);header.writeUInt16LE(32,at+6);
    header.writeUInt32LE(entries[i].length,at+8);header.writeUInt32LE(offset,at+12);
    offset += entries[i].length;
  });
  await fs.writeFile(path.join(assets,'app-icon.ico'),Buffer.concat([header,...entries]));
  window.destroy();
  console.log('Generated PNG 1024px, macOS ICNS and Windows ICO (16/24/32/48/64/128/256px).');
  app.quit();
}).catch(error => {console.error(error);app.exit(1);});
app.on('quit', () => syncFs.rmSync(scratch,{recursive:true,force:true}));
