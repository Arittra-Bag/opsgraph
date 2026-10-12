'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { definition, policyFields } = require('../../src/opsgraph/web/static/workflow-authoring.js');
const template = { tools: [{tool:'core.sql.select',settings:{max_rows:10,timeout_ms:800,allowed_schemas:['approved']}}], workflow:{version:1,max_queries:1,follow_up_queries:false,max_findings:4,max_limitations:3}, risk:'read_only', egress:'forbidden' };
const fields = { id:'custom-workflow',version:'1.0.0',name:'My workflow',purpose:'Observe narrowly scoped records.',max_queries:2,max_findings:2,max_limitations:1,follow_up_queries:true,external_model:false };

test('builder reuses policy-bounded tools without mutating the received starting definition', () => {
  const output = definition(template, fields);
  assert.deepEqual(output.tools, template.tools); assert.deepEqual(template.workflow.max_queries,1);
  assert.equal(output.workflow.max_queries,2); assert.equal(output.workflow.follow_up_queries,true);
  assert.equal(output.egress,'forbidden'); assert.equal(output.risk,'read_only');
});
for (const value of [0,4,1.5,'abc']) test(`invalid query budget ${value} is rejected`, () => {
  assert.throws(() => definition(template,{...fields,max_queries:value}), /integer from 1 to 3/);
});
test('plain policy summary does not invent deployment table or model permissions', () => {
  const summary = policyFields({default:'deny',obligations:{max_rows:20,timeout_ms:500,allowed_schemas:['public'],allowed_tables:[]}});
  assert.match(summary['Deployment table ceiling'],/selected and approved in Sources/);
  assert.equal(summary['Query timeout'],'500 ms');
  assert.match(summary['External model permission'],/does not grant permission/);
});
