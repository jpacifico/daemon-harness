'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {blocked,details}=require('./removal-diagnostics.cjs');
test('removal reports exclude arbitrary messages, paths, XML and keys',()=>{
 const error=new Error('removal.services');
 error.removalDiagnostic={stage:'task_xml',hresult:-2147024891,exit_code:1,path:'SECRET',xml:'SECRET',token:'SECRET'};
 const r=blocked(error,'review',{revision:'a'.repeat(40),secret:'SECRET'});
 assert.equal(r.blocked,true);assert.equal(r.diagnostic.stage,'task_xml');
 assert.equal(r.diagnostic.hresult,-2147024891);assert(!JSON.stringify(r).includes('SECRET'));
 assert.deepEqual(details({stage:'SECRET',hresult:'SECRET',exit_code:Infinity}),{stage:'unknown'});
 const unknown=blocked(new Error('secret /path/token'),'SECRET',{revision:'SECRET'});
 assert.equal(unknown.code,'removal.unconfirmed');assert(!JSON.stringify(unknown).includes('SECRET'));
});
test('apply failures are distinguishable from no-mutation review failures',()=>{
 assert.equal(blocked(new Error('removal.recovery'),'apply').diagnostic.phase,'apply');
 assert.equal(blocked(new Error('removal.services'),'review').diagnostic.phase,'review');
});
test('cleanup OS numbers and precise stages survive without exception contents',()=>{
 const error=new Error('removal.recovery');
 error.removalDiagnostic={stage:'file_open',winerror:32,errno:13,message:'SECRET',filename:'SECRET'};
 const report=blocked(error,'apply');
 assert.equal(report.diagnostic.stage,'file_open');assert.equal(report.diagnostic.winerror,32);
 assert.equal(report.diagnostic.errno,13);assert(!JSON.stringify(report).includes('SECRET'));
 assert.deepEqual(details({stage:'program_identity',winerror:'32',errno:NaN}),{stage:'program_identity'});
});
