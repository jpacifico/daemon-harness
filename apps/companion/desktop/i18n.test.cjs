"use strict";
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const catalog = require('./locales/messages.json');
const {createLocalizer} = require('./i18n.js');
const {Preferences} = require('./preferences.cjs');
const locale = createLocalizer(catalog);
test('English and French catalogs have identical named arguments and complete values', () => {
  const sources = new Map();
  for (const [key, row] of Object.entries(catalog)) {
    assert.ok(row.en?.trim() && row.fr?.trim(), key);
    const args = value => [...value.matchAll(/\{([a-zA-Z_]+)\}/g)].map(m => m[1]).sort();
    assert.deepEqual(args(row.en), args(row.fr), key);
    if (sources.has(row.en)) assert.equal(row.fr, sources.get(row.en), key);
    sources.set(row.en, row.fr);
    assert.equal(locale.t(key), row.en.replace(/\bHermes\b(?! Agent)/g, 'Hermes Agent').replace(/ — /g, ': '));
  }
});
test('Source labels do not alter answers, paths, model IDs or technical evidence', () => {
  const cases = [
    ['Please review: Daemon name must be 1–40 letters, numbers, spaces, dots, apostrophes or hyphens.', 'À vérifier : Le nom du daemon doit contenir de 1 à 40 lettres, chiffres, espaces, points, apostrophes ou tirets.'],
    ['Destination: C:\\Users\\Élodie\\Projects', 'Destination : C:\\Users\\Élodie\\Projects'],
    ['Selected: Astra (gpt-6-astra)', 'Sélection : Astra (gpt-6-astra)'],
    ['Downloading local memory search: 46%', 'Téléchargement du modèle de recherche mémoire : 46 %'],
    ['Token accepted — Telegram verified @Example_bot. Installing your daemon next.', 'Token accepté: Telegram a vérifié @Example_bot. Installation du daemon à présent.'],
    ['Projects, Areas, Resources, Garden and Archives.', 'Projects, Areas, Resources, Garden and Archives.'],
    ['WinError 123: C:\\Users\\Élodie\\data', 'WinError 123: C:\\Users\\Élodie\\data']
  ];
  for (const [source, translated] of cases) { assert.equal(locale.message(source, 'fr'), translated); assert.equal(locale.message(source), source.replace(/ — /g, ': ')); }
  assert.equal(locale.message('Planned changes:\n\n1. Review and confirm every change before writing.', 'fr'), 'Modifications prévues :\n\n1. Vérifier et confirmer chaque modification avant écriture.');
  assert.equal(locale.message('Set up Élodie on Hermes Agent\nPlanned changes:', 'fr'), 'Installer Élodie sur Hermes Agent\nModifications prévues :');
});
test('Preference defaults to English, persists explicitly, rejects unsupported input and recovers corrupt settings', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'companion-language-'));
  try {
    const settings = new Preferences(root);
    assert.equal(await settings.load(), 'en');
    await settings.set('fr');
    assert.equal(await new Preferences(root).load(), 'fr');
    await assert.rejects(settings.set('../../fr'));
    await Promise.all([settings.set('en'), settings.set('fr')]);
    assert.equal(await new Preferences(root).load(), 'fr');
    assert.deepEqual(JSON.parse(await fs.readFile(settings.file,'utf8')), {schema:1,language:'fr',theme:'light',background:false,openAtLogin:false});
    assert.deepEqual(await fs.readdir(root), ['display-preferences.json']);
    await fs.writeFile(settings.file, '{');
    assert.equal(await new Preferences(root).load(), 'en');
  } finally { await fs.rm(root,{recursive:true,force:true}); }
});

test('Hermes Agent branding changes catalog literals, never user content or engine plans', () => {
  assert.equal(locale.message('Install Hermes'), 'Install Hermes Agent');
  assert.equal(locale.message('Install Hermes', 'fr'), 'Installer Hermes Agent');
  assert.equal(locale.message('Set up Hermes on Hermes Agent'), 'Set up Hermes on Hermes Agent');
  assert.equal(locale.message('Set up Hermes on Hermes Agent', 'fr'), 'Installer Hermes sur Hermes Agent');
  for (const text of ['Hermes', '/Users/Hermes/config.yaml', 'Unknown diagnostic: Hermes', '{"host":"hermes"}']) {
    assert.equal(locale.message(text), text);
  }
});
test('a known download cause is localized while unknown technical causes stay exact', () => {
  const limited = 'Installation cause: ' + catalog['host.download_limited'].en.replace('{service}', 'GitHub');
  assert.equal(locale.message(limited, 'fr'), 'Cause de l’échec : ' + catalog['host.download_limited'].fr.replace('{service}', 'GitHub'));
  const waiting = catalog['host.download_waiting'].en.replace('{service}', 'GitHub').replace('{seconds}', '45').replace('{attempt}', '4').replace('{total}', '7');
  assert.equal(locale.message(waiting, 'fr'), 'GitHub ne répond pas. Nouvel essai dans 45 secondes (essai 4 sur 7)…');
  assert.equal(locale.message('Installation cause: HTTP Error 404: Not Found', 'fr'),
    'Cause de l’échec : HTTP Error 404: Not Found');
});
test('a failure names the outside service whose status page the app consults', () => {
  assert.deepEqual(locale.statusOf('Anthropic is not answering right now. Your key is kept: try again in a few minutes.'), {name: 'Anthropic'});
  assert.deepEqual(locale.statusOf('No response from GitHub. Retrying in 45 seconds (attempt 4 of 7)...'), {name: 'GitHub'});
  assert.deepEqual(locale.statusOf(locale.t('update.unreachable')), {id: 'github'});
  assert.deepEqual(locale.statusOf(locale.t('drive.uploadFailed')), {id: 'google'});
  // Nested in an installation cause, the service is still the one named, never the prefix.
  const cause = 'Installation cause: No response from GitHub for about five minutes, usually a temporary problem on its side or no Internet connection. The steps already done are kept: wait a few minutes, then try again.';
  assert.deepEqual(locale.statusOf(cause), {name: 'GitHub'});
  assert.match(locale.message(cause, 'fr'), /^Cause de l’échec : GitHub ne répond plus depuis environ cinq minutes/);
  // A rejected key or a usage limit is about the user's own access: no status page.
  assert.equal(locale.statusOf('OpenAI rejected this API key or its permissions. Check the key and try again.'), null);
  assert.equal(locale.statusOf('OpenAI reports a usage limit. Check your API balance with OpenAI, or try again later.'), null);
  assert.equal(locale.statusOf('Setup did not finish'), null);
  assert.equal(locale.message('Mistral AI returned an unexpected response. Your key is kept: try again in a few minutes.', 'fr'),
    'Mistral AI a renvoyé une réponse inattendue. Votre clé est conservée : réessayez dans quelques minutes.');
});
