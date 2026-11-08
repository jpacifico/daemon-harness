'use strict';
// Reads the public status page of a service the app could not reach, so a
// failure can say whether that service reports an incident. Only the fixed
// addresses in services.js are requested, with no user data, and only while a
// failure is on screen. The answer is shown as the service states it; it never
// decides a setup step or a retry.
const {SERVICES} = require('./services.js');

const MAX_BYTES = 2 * 1024 * 1024, TIMEOUT = 8000, MAX_ITEMS = 3, MAX_TEXT = 160;
// A clear answer holds for two minutes; an unreachable page is asked again sooner.
const TTL = 120000, UNREACHABLE_TTL = 15000;
const RESOLVED = new Set(['resolved', 'postmortem', 'completed']);

const text = value => String(value || '').replace(/[\u0000-\u001f\u007f]+/g, ' ').replace(/\*\*/g, '')
  .replace(/\s+/g, ' ').trim().slice(0, MAX_TEXT);
const names = list => (Array.isArray(list) ? list : []).map(item => text(item?.name ?? item?.title)).filter(Boolean);

// Atlassian Statuspage format, also served by OpenAI's page: unresolved
// incidents with an impact, and components that are not operational.
function statuspage(doc, service) {
  if (!doc || typeof doc !== 'object' || !doc.page) throw new Error('Not a status page.');
  const ignored = new Set(service.ignore || []);
  const related = components => !components.length || components.some(name => !ignored.has(name));
  const incidents = (Array.isArray(doc.incidents) ? doc.incidents : [])
    .filter(i => i && !RESOLVED.has(i.status) && i.impact !== 'none' && related(names(i.components)))
    .map(i => text(i.name)).filter(Boolean);
  const disruptions = (Array.isArray(doc.components) ? doc.components : [])
    .filter(c => c && !c.group && typeof c.status === 'string' && c.status !== 'operational' && !ignored.has(text(c.name)))
    .map(c => text(c.name)).filter(Boolean);
  return {incidents, disruptions};
}

// Google Workspace dashboard: incidents without an end that affect the product.
function google(doc, service) {
  if (!Array.isArray(doc)) throw new Error('Not a status feed.');
  const products = new Set(service.products);
  const summary = description => text(String(description || '').split('\n')
    .map(line => line.replace(/\*\*[^*]*:\*\*/g, '').replace(/[#*]/g, '').trim()).find(Boolean));
  const incidents = doc.filter(i => i && !i.end && [i.service_name, ...names(i.affected_products)].some(name => products.has(name)))
    .map(i => summary(i.external_desc)).filter(Boolean);
  return {incidents, disruptions: []};
}

class ServiceStatus {
  constructor({fetch, now = Date.now} = {}) {
    this.fetch = fetch; this.now = now; this.cache = new Map(); this.pending = new Map();
  }
  async check(id) {
    const service = typeof id === 'string' && Object.hasOwn(SERVICES, id) ? SERVICES[id] : null;
    if (!service) throw new Error('Unknown service.');
    const base = {id, name: service.name, page: service.page || null};
    if (!service.feed) return {...base, state: 'none'};
    const hit = this.cache.get(id);
    if (hit && this.now() < hit.until) return hit.value;
    if (this.pending.has(id)) return this.pending.get(id);
    const request = this.read(service).then(found => ({...base, ...found}), () => ({...base, state: 'unreachable'}))
      .then(value => {
        this.cache.set(id, {value, until: this.now() + (value.state === 'unreachable' ? UNREACHABLE_TTL : TTL)});
        this.pending.delete(id);
        return value;
      });
    this.pending.set(id, request);
    return request;
  }
  async read(service) {
    const response = await this.fetch(service.feed, {redirect: 'error', cache: 'no-store',
      headers: {accept: 'application/json'}, signal: AbortSignal.timeout(TIMEOUT)});
    if (!response.ok) throw new Error('Status page unavailable.');
    if (Number(response.headers?.get?.('content-length')) > MAX_BYTES) throw new Error('Status page too large.');
    const raw = await response.text();
    if (raw.length > MAX_BYTES) throw new Error('Status page too large.');
    const found = (service.kind === 'google' ? google : statuspage)(JSON.parse(raw), service);
    const incidents = [...new Set(found.incidents)].slice(0, MAX_ITEMS);
    const disruptions = [...new Set(found.disruptions)].slice(0, MAX_ITEMS);
    return {state: incidents.length || disruptions.length ? 'incident' : 'clear', incidents, disruptions};
  }
}

module.exports = {ServiceStatus, statuspage, google};
