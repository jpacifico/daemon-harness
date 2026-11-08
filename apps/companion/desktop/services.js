/* The outside services the app depends on, shared by the window and the main
   process. A failure message names its service; when that service publishes a
   status page the app can read, the app consults it to say whether an incident
   is in progress. Display and lookup only: never used to decide a setup step. */
'use strict';
(function (scope) {
  // `ignore` lists status page components unrelated to what the app uses: an
  // incident confined to them is not shown. Any other incident, including one
  // with no component or a component added later, is shown.
  const SERVICES = Object.freeze({
    github: Object.freeze({name: 'GitHub', page: 'https://www.githubstatus.com',
      feed: 'https://www.githubstatus.com/api/v2/summary.json', kind: 'statuspage',
      ignore: ['Webhooks', 'Issues', 'Pull Requests', 'Actions', 'Pages', 'Copilot', 'Codespaces',
        'Copilot AI Model Providers', 'Visit www.githubstatus.com for more information']}),
    openai: Object.freeze({name: 'OpenAI', page: 'https://status.openai.com',
      feed: 'https://status.openai.com/api/v2/summary.json', kind: 'statuspage',
      ignore: ['Fine-tuning', 'Embeddings', 'Images', 'Batch', 'Audio', 'Moderations', 'Realtime', 'Files', 'Live',
        'ChatGPT Work', 'Compliance API', 'Search', 'File uploads', 'Voice mode', 'GPTs', 'Image Generation',
        'Deep Research', 'Decisions', 'Agents', 'Sora']}),
    anthropic: Object.freeze({name: 'Anthropic', page: 'https://status.claude.com',
      feed: 'https://status.claude.com/api/v2/summary.json', kind: 'statuspage',
      ignore: ['claude.ai', 'Claude Code', 'Claude Cowork', 'Claude for Government']}),
    mistral: Object.freeze({name: 'Mistral AI', page: 'https://status.mistral.ai',
      feed: 'https://status.mistral.ai/api/v2/summary.json', kind: 'statuspage',
      ignore: ['OCR API', 'Fine-Tuning API', 'Audio API', 'Batch API', 'Workflows API', 'Files API', 'Agents API',
        'Integrations API']}),
    google: Object.freeze({name: 'Google Drive', page: 'https://www.google.com/appsstatus/dashboard/',
      feed: 'https://www.google.com/appsstatus/dashboard/incidents.json', kind: 'google',
      products: ['Google Drive', 'Multiple Products']}),
    // No status page the app can read: the message names them, nothing more.
    telegram: Object.freeze({name: 'Telegram'}),
    ollama: Object.freeze({name: 'Ollama'}),
    nodejs: Object.freeze({name: 'Node.js'}),
    xai: Object.freeze({name: 'xAI'}),
  });
  // Names as failure messages write them. ChatGPT sign-in and models are OpenAI's.
  const NAMES = new Map([['ChatGPT', 'openai'], ['Google', 'google'], ['SpaceXSI', 'xai'],
    ...Object.entries(SERVICES).map(([id, service]) => [service.name, id])]);
  const serviceId = name => NAMES.get(String(name || '').trim()) || null;
  const readable = id => Boolean(SERVICES[id]?.feed);
  const api = {SERVICES, serviceId, readable};
  if (typeof module === 'object') module.exports = api;
  else scope.daemonServices = api;
})(globalThis);
