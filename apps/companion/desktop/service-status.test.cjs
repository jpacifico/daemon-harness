'use strict';
// Status pages of outside services, against synthetic feeds; no network.
const test = require('node:test');
const assert = require('node:assert/strict');
const {ServiceStatus, statuspage, google} = require('./service-status.cjs');
const {SERVICES, serviceId, readable} = require('./services.js');
const catalog = require('./locales/messages.json');

const page = (incidents = [], components = []) => ({page: {name: 'Synthetic'}, status: {indicator: 'none'}, incidents, components});
const incident = (name, extra = {}) => ({name, status: 'investigating', impact: 'minor', components: [], ...extra});
const served = (body, {status = 200, length = null} = {}) => async () => ({ok: status >= 200 && status < 300, status,
  headers: {get: name => name === 'content-length' && length !== null ? String(length) : null},
  text: async () => typeof body === 'string' ? body : JSON.stringify(body)});

test('an unresolved incident with an impact is shown with its own name', () => {
  const found = statuspage(page([incident('Increase in timeouts for some unauthenticated users', {components: [{name: 'API Requests'}]})]), SERVICES.github);
  assert.deepEqual(found, {incidents: ['Increase in timeouts for some unauthenticated users'], disruptions: []});
});

test('incidents without impact, resolved, or confined to unrelated components are not shown', () => {
  const found = statuspage(page([
    incident('Card payments', {impact: 'none'}),
    incident('Yesterday', {status: 'resolved'}),
    incident('Copilot is slow', {components: [{name: 'Copilot'}, {name: 'Codespaces'}]}),
    incident('Copilot and Git', {components: [{name: 'Copilot'}, {name: 'Git Operations'}]}),
  ]), SERVICES.github);
  assert.deepEqual(found.incidents, ['Copilot and Git']);
});

test('a related component that is not operational is a disruption; groups and unrelated ones are not', () => {
  const found = statuspage(page([], [
    {name: 'Git Operations', status: 'partial_outage'}, {name: 'Actions', status: 'major_outage'},
    {name: 'Group', status: 'degraded_performance', group: true}, {name: 'API Requests', status: 'operational'},
  ]), SERVICES.github);
  assert.deepEqual(found, {incidents: [], disruptions: ['Git Operations']});
});

test('names from a status page are plain bounded text', () => {
  const found = statuspage(page([incident('**Bold**\n\u0007 ' + 'x'.repeat(400))]), SERVICES.openai);
  assert.equal(found.incidents[0].startsWith('Bold x'), true);
  assert.equal(found.incidents[0].length, 160);
});

test('a Google Drive incident counts only while it has no end', () => {
  const found = google([
    {service_name: 'Google Drive', end: null, external_desc: '**Summary:**\nWe are investigating errors with Google Drive\n**Description:**\nMore.'},
    {service_name: 'Google Drive', end: '2026-09-16T14:41:00+00:00', external_desc: '**Summary:**\nOld'},
    {service_name: 'Gmail', end: null, external_desc: '**Summary:**\nMail only'},
    {service_name: 'Multiple Products', end: null, affected_products: [{title: 'Gmail'}], external_desc: '**Title:**\nSeveral products'},
  ], SERVICES.google);
  assert.deepEqual(found.incidents, ['We are investigating errors with Google Drive', 'Several products']);
});

test('a check reads the fixed feed once, then answers from its cache', async () => {
  const seen = [];
  const fetch = async (url, options) => { seen.push([url, options.redirect]); return served(page([incident('Down')]))(); };
  let now = 1000;
  const status = new ServiceStatus({fetch, now: () => now});
  const [first, second] = await Promise.all([status.check('github'), status.check('github')]);
  assert.deepEqual(first, {id: 'github', name: 'GitHub', page: 'https://www.githubstatus.com', state: 'incident', incidents: ['Down'], disruptions: []});
  assert.equal(second, first);
  assert.deepEqual(seen, [['https://www.githubstatus.com/api/v2/summary.json', 'error']]);
  now += 60000; await status.check('github'); assert.equal(seen.length, 1);
  now += 61000; await status.check('github'); assert.equal(seen.length, 2);
});

test('no incident is clear; an unreadable page is unreachable and asked again sooner', async () => {
  assert.equal((await new ServiceStatus({fetch: served(page())}).check('openai')).state, 'clear');
  for (const fetch of [async () => { throw new TypeError('offline'); }, served('<html>', {}), served(page(), {status: 503}),
    served(page(), {length: 3 * 1024 * 1024}), served({not: 'a status page'})]) {
    assert.equal((await new ServiceStatus({fetch}).check('anthropic')).state, 'unreachable');
  }
  let calls = 0, now = 0;
  const status = new ServiceStatus({fetch: async () => { calls++; throw new Error('offline'); }, now: () => now});
  await status.check('github'); now += 16000; await status.check('github');
  assert.equal(calls, 2);
});

test('a service without a readable page is named only, and an unknown id is refused', async () => {
  const status = new ServiceStatus({fetch: async () => { throw new Error('never called'); }});
  assert.deepEqual(await status.check('telegram'), {id: 'telegram', name: 'Telegram', page: null, state: 'none'});
  await assert.rejects(status.check('toString'), /Unknown service/);
  await assert.rejects(status.check('https://example.com'), /Unknown service/);
});

test('failure messages name services the registry knows', () => {
  assert.equal(serviceId('GitHub'), 'github');
  assert.equal(serviceId('ChatGPT'), 'openai');
  assert.equal(serviceId('SpaceXSI'), 'xai');
  assert.equal(serviceId('The download service'), null);
  assert.equal(readable('github') && readable('google') && !readable('telegram'), true);
  for (const [key, row] of Object.entries(catalog)) {
    if (!row.status) continue;
    const argument = /^\{([a-zA-Z_]+)\}$/.exec(row.status);
    if (argument) assert.ok(row.template && row.en.includes(row.status), key);
    else assert.ok(Object.hasOwn(SERVICES, row.status), key);
  }
  for (const service of Object.values(SERVICES)) {
    if (service.feed) assert.match(service.feed, /^https:\/\/[a-z.]+\/[a-z0-9/._-]+\.json$/);
    if (service.page) assert.match(service.page, /^https:\/\//);
  }
});
