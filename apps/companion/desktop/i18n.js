/* Display-only catalog adapter. Never used for engine routing or answers. */
'use strict';
(function (scope) {
  function createLocalizer(catalog) {
    const exact = new Map(), patterns = [];
    const quote = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    for (const [key, entry] of Object.entries(catalog)) {
      if (!entry.template) exact.set(entry.en, key);
      else {
        const names = [];
        const parts = entry.en.split(/(\{[a-zA-Z_]+\})/g).map(part => {
          if (!/^\{[a-zA-Z_]+\}$/.test(part)) return quote(part);
          const name = part.slice(1, -1);
          names.push(name);
          // A service is a short name, never a sentence: a message nested in
          // another one ("Installation cause: …") cannot be taken for it.
          return name === 'service' ? '([A-Z][A-Za-z0-9. ]{0,39})' : '([^\\r\\n]+?)';
        });
        patterns.push({key, names, re: new RegExp('^' + parts.join('') + '$')});
      }
    }
    function t(key, language = 'en', args = {}) {
      const entry = catalog[key];
      if (!entry) throw new Error('Unknown display message key: ' + key);
      // Update catalog wording before interpolation, never user values or the
      // engine's persisted plans and confirmation hashes.
      return entry[language === 'fr' ? 'fr' : 'en'].replace(/\bHermes\b(?! Agent)/g, 'Hermes Agent').replace(/ — /g, ': ').replace(/\{([a-zA-Z_]+)\}/g, (match, name) =>
        Object.hasOwn(args, name) ? String(args[name]) : match);
    }
    function identify(text) {
      if (exact.has(text)) return {key: exact.get(text), args: {}};
      for (const {key, names, re} of patterns) {
        const match = re.exec(text);
        if (match) return {key, args: Object.fromEntries(names.map((name, i) => [name, match[i + 1]]))};
      }
      return null;
    }
    function message(value, language = 'en') {
      const text = String(value);
      const trimmed = text.trim();
      const found = identify(trimmed);
      if (found) {
        // Only explicitly typed display parameters are translated. Never names,
        // pronouns, paths, models, diagnostic bodies or arbitrary user data.
        const fields = {'review.agreement':['gender'], 'review.provider':['provider'],
          'diagnostic.step':['step','phase'], 'diagnostic.review':['detail'],
          'diagnostic.stopped':['detail'], 'diagnostic.cause':['detail'], 'guide.page':['title']}[found.key] || [];
        for (const field of fields) {
          const key = exact.get(found.args[field]);
          if (key) found.args[field] = t(key, language);
          else {
            // A nested catalog template (a download cause naming its service)
            // is translated around its own values, which stay exact.
            const inner = patterns.find(p => p.re.test(found.args[field]));
            if (inner) found.args[field] = t(inner.key, language, Object.fromEntries(inner.names.map((name, i) => [name, inner.re.exec(found.args[field])[i + 1]])));
          }
        }
        return text.replace(trimmed, t(found.key, language, found.args));
      }
      if (text.includes('\n')) return text.split('\n').map(line => message(line, language)).join('\n');
      const numbered = /^(\s*(?:\d+\. |\- ))(.+)$/.exec(text);
      if (numbered) return numbered[1] + message(numbered[2], language);
      return text; // external identifiers / technical diagnostics remain exact
    }
    function localizeElement(root, language) {
      const walker = root.ownerDocument.createTreeWalker(root, 4);
      const nodes = [];
      while (walker.nextNode()) nodes.push(walker.currentNode);
      for (const node of nodes) {
        if (!node.parentElement.closest('[data-original], .bot-name, option')) node.nodeValue = message(node.nodeValue, language);
      }
      for (const element of root.querySelectorAll('[aria-label]')) element.setAttribute('aria-label', message(element.getAttribute('aria-label'), language));
    }
    // The outside service a failure message names, for its status page: a fixed
    // id from the catalog, or the name a template received. A message nested in
    // another one (an installation cause) is followed.
    function statusOf(value, depth = 0) {
      const found = identify(String(value).trim());
      if (!found) return null;
      const status = catalog[found.key].status, argument = /^\{([a-zA-Z_]+)\}$/.exec(status || '');
      if (argument) return found.args[argument[1]] ? {name: found.args[argument[1]]} : null;
      if (status) return {id: status};
      if (depth > 1) return null;
      for (const arg of Object.values(found.args)) { const inner = statusOf(arg, depth + 1); if (inner) return inner; }
      return null;
    }
    const isConfirmation = value => catalog[identify(String(value).trim())?.key]?.confirmation === true;
    const isEmphasis = value => catalog[identify(String(value).trim())?.key]?.emphasis === true;
    return {t, identify, message, localizeElement, isConfirmation, isEmphasis, statusOf};
  }
  if (typeof module === 'object') module.exports = {createLocalizer};
  else scope.createLocalizer = createLocalizer;
})(globalThis);
