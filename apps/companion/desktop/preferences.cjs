'use strict';
const fs = require('node:fs/promises');
const path = require('node:path');
class Preferences {
  constructor(directory) {
    this.file = path.join(directory, 'display-preferences.json');
    this.language = 'en'; this.theme = 'light'; this.pending = Promise.resolve();
    // Running in the background keeps daily backups going with the window closed.
    this.background = false; this.openAtLogin = false;
  }
  async load() {
    try {
      const info = await fs.lstat(this.file);
      if (!info.isSymbolicLink() && info.isFile() && info.size < 1024) {
        const data = JSON.parse(await fs.readFile(this.file, 'utf8'));
        if (data.schema === 1) {
          if (['en', 'fr'].includes(data.language)) this.language = data.language;
          if (['light', 'dark', 'system'].includes(data.theme)) this.theme = data.theme;
          if (typeof data.background === 'boolean') this.background = data.background;
          if (typeof data.openAtLogin === 'boolean') this.openAtLogin = data.openAtLogin;
        }
      }
    } catch { /* Missing/corrupt display settings default to English and Light. */ }
    return this.language;
  }
  set(language) {
    if (!['en', 'fr'].includes(language)) return Promise.reject(new Error('Unsupported display language.'));
    return this.update({language}).then(() => this.language);
  }
  setTheme(theme) {
    if (!['light', 'dark', 'system'].includes(theme)) return Promise.reject(new Error('Unsupported display theme.'));
    return this.update({theme}).then(() => this.theme);
  }
  setBackground(background, openAtLogin) {
    if (typeof background !== 'boolean' || typeof openAtLogin !== 'boolean') return Promise.reject(new Error('Unsupported background setting.'));
    return this.update({background, openAtLogin: background && openAtLogin});
  }
  update(change) {
    const task = this.pending.then(async () => {
      // Merge at execution time: concurrent language/theme writes preserve both.
      const next = {schema: 1, language: this.language, theme: this.theme, background: this.background, openAtLogin: this.openAtLogin, ...change};
      await fs.mkdir(path.dirname(this.file), {recursive: true});
      const temporary = this.file + '.' + require('node:crypto').randomUUID() + '.tmp';
      await fs.writeFile(temporary, JSON.stringify(next) + '\n', {mode: 0o600, flag: 'wx'});
      try { await fs.rename(temporary, this.file); }
      catch (error) { await fs.unlink(temporary).catch(() => {}); throw error; }
      this.language = next.language; this.theme = next.theme; this.background = next.background; this.openAtLogin = next.openAtLogin;
    });
    this.pending = task.catch(() => {});
    return task;
  }
}
module.exports = {Preferences};
