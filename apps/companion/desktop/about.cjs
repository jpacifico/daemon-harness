'use strict';
// Fixed, user-approved destinations. No arbitrary URL or private data is accepted.
const project = 'https://github.com/jpacifico/daemon-harness-canonical';
const destinations = Object.freeze({project, contact: project + '/issues', guide: project + '/blob/main/docs/getting-started.md', obsidian:'https://obsidian.md/download', telegram:'https://web.telegram.org/k/'});
function destination(key) {
  if (!Object.hasOwn(destinations, key)) throw new Error('Unsupported project link.');
  return destinations[key];
}
function information({version, revision}) {
  return {author: 'Jonathan Pacifico', version,
    revision: /^[a-f0-9]{40}$/.test(revision || '') ? revision.slice(0, 8) : null};
}
module.exports = {destination, information};
