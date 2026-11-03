'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {groupDaemons}=require('./daemon-cards.js');
test('shared canonical identity groups hosts but names and vault alone do not',()=>{
 const row=(id,runtime,identity='vault-a',name='Aster')=>({id,host:'hermes',daemon:{name,id:identity,runtime}});
 const result=groupDaemons([row('a','/a/.daemon'),row('b','/a/.daemon'),row('c','/b/.daemon','vault-b'),row('d','/a/other-runtime'),row('e',null,null),row('f',null,null),{id:'g',daemon:null}]);
 assert.deepEqual(result.daemons.map(d=>d.hosts.map(h=>h.id)),[['a','b'],['c'],['d'],['e'],['f']]);
 assert.deepEqual(result.hosts.map(h=>h.id),['g']);assert.equal(result.daemons[0].name,'Aster');
 assert.deepEqual(groupDaemons([]),{daemons:[],hosts:[]});
});
