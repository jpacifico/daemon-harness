"use strict";
const test = require('node:test');
const assert = require('node:assert/strict');
const about = require('./about.cjs');
test('About exposes approved public metadata and only two fixed browser destinations', () => {
  assert.equal(about.destination('project'),'https://github.com/jpacifico/daemon-harness');
  assert.equal(about.destination('contact'),'https://github.com/jpacifico/daemon-harness/issues');
  for(const invalid of ['https://example.org','file:///etc/passwd','__proto__','constructor',null])assert.throws(()=>about.destination(invalid));
  assert.deepEqual(about.information({version:'0.1.0',revision:'a'.repeat(40),privateHome:'/private/profile'}),
    {author:'Jonathan Pacifico',version:'0.1.0',revision:'aaaaaaaa'});
  assert.equal(about.information({version:'0.1.0',revision:'invalid/path'}).revision,null);
});
