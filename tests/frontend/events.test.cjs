'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { createSSEParser } = require('../../src/opsgraph/web/static/events.js');

test('split transport chunks retain exactly one complete event', () => {
  const events = [];
  const parser = createSSEParser(event => events.push(event));
  parser.push('id: 12\nevent: progress\nda');
  parser.push('ta: {"run_id":"run-1","id":12}\n');
  assert.equal(events.length, 0);
  parser.push('\n');
  assert.deepEqual(events, [{ id: '12', type: 'progress', data: '{"run_id":"run-1","id":12}' }]);
});

test('CRLF split across chunks and heartbeat comments do not invent events', () => {
  const events = [];
  const parser = createSSEParser(event => events.push(event));
  parser.push(': heartbeat\r');
  parser.push('\n\r\nid: 3\r\nevent: progress\r\ndata: {"id":3}\r');
  parser.push('\n\r\n');
  assert.deepEqual(events, [{ id: '3', type: 'progress', data: '{"id":3}' }]);
});

test('multiple events preserve order, multiline data and independent identifiers', () => {
  const events = [];
  const parser = createSSEParser(event => events.push(event));
  parser.push('id: 1\ndata: first\ndata: second\n\nid: 2\nevent: progress\ndata: third\n\n');
  assert.deepEqual(events, [{ id: '1', type: 'message', data: 'first\nsecond' }, { id: '2', type: 'progress', data: 'third' }]);
});

test('disconnect before event delimiter discards incomplete event for replay', () => {
  const events = [];
  const parser = createSSEParser(event => events.push(event));
  parser.push('id: 4\ndata: {"incomplete":true}\n');
  parser.finish();
  assert.deepEqual(events, []);
  const reconnected = createSSEParser(event => events.push(event));
  reconnected.push('id: 4\ndata: {"complete":true}\n\n');
  assert.deepEqual(events, [{ id: '4', type: 'message', data: '{"complete":true}' }]);
});

test('UTF-8 text decoder across byte boundaries preserves evidence text', () => {
  const events = [];
  const parser = createSSEParser(event => events.push(event));
  const decoder = new TextDecoder();
  const bytes = new TextEncoder().encode('id: 1\ndata: {"source":"दत्तांश 🧪"}\n\n');
  for (const byte of bytes) parser.push(decoder.decode(Uint8Array.of(byte), { stream: true }));
  parser.push(decoder.decode());
  assert.equal(JSON.parse(events[0].data).source, 'दत्तांश 🧪');
});
