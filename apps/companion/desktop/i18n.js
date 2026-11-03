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
          names.push(part.slice(1, -1)); return '([^\\r\\n]+?)';
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
    const isConfirmation = value => catalog[identify(String(value).trim())?.key]?.confirmation === true;
    const isEmphasis = value => catalog[identify(String(value).trim())?.key]?.emphasis === true;
    return {t, identify, message, localizeElement, isConfirmation, isEmphasis};
  }
  if (typeof module === 'object') module.exports = {createLocalizer};
  else scope.createLocalizer = createLocalizer;
})(globalThis);
