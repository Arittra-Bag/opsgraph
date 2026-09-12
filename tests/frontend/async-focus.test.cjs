'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Exercise the exact browser helper with a small DOM/event fixture. Browser keyboard
// acceptance remains separate; this verifies late responses cannot steal moved focus.
const source = fs.readFileSync(path.join(__dirname, '../../src/opsgraph/web/static/app.js'), 'utf8');
const helper = source.match(/  function guardAsyncFocus\(control, fallback = null\) \{[\s\S]*?(?=\n  function errorMessage)/)?.[0];
assert.ok(helper, 'browser async focus helper must remain testable');
function fixture() {
  const listeners = new Map();
  const document = {
    body: {}, activeElement: null,
    addEventListener(type, listener) { listeners.set(type, listener); },
    removeEventListener(type) { listeners.delete(type); },
  };
  function control({ disabled = false, visible = true } = {}) {
    const attributes = new Map();
    return { disabled, isConnected: true, getClientRects: () => visible ? [{}] : [], hasAttribute: name => attributes.has(name), setAttribute: (name, value) => attributes.set(name, value), focus() { document.activeElement = this; } };
  }
  const guard = vm.runInNewContext(`(${helper.trim()})`, { document });
  const emit = (type, target) => listeners.get(type)?.({ type, target });
  return { document, control, guard, emit, listeners };
}

test('a disabled keyboard action returns focus after completion when focus fell to body', () => {
  const f = fixture(), button = f.control(); f.document.activeElement = button;
  const restore = f.guard(button); button.disabled = true; f.document.activeElement = f.document.body;
  button.disabled = false; restore();
  assert.equal(f.document.activeElement, button);
  assert.equal(f.listeners.size, 0);
});

test('a hidden or still-disabled action falls back to the visible operation status', () => {
  for (const setup of [{ disabled: true }, { visible: false }]) {
    const f = fixture(), button = f.control(setup), status = f.control(); f.document.activeElement = button;
    const restore = f.guard(button, status); f.document.activeElement = f.document.body; restore();
    assert.equal(f.document.activeElement, status);
    assert.equal(status.hasAttribute('tabindex'), true);
  }
});

test('moving focus to another control prevents late action completion from stealing it', () => {
  const f = fixture(), button = f.control(), other = f.control(); f.document.activeElement = button;
  const restore = f.guard(button); f.document.activeElement = other; f.emit('focusin', other); restore();
  assert.equal(f.document.activeElement, other);
  assert.equal(f.listeners.size, 0);
});

test('keyboard or pointer navigation intent is respected even if focus is later body', () => {
  for (const event of ['keydown', 'pointerdown']) {
    const f = fixture(), button = f.control(); f.document.activeElement = button;
    const restore = f.guard(button); f.document.activeElement = f.document.body; f.emit(event, f.document.body); restore();
    assert.equal(f.document.activeElement, f.document.body);
  }
});

test('actions that never owned focus do not move it; detached controls are not focused', () => {
  const f = fixture(), button = f.control(), other = f.control(); f.document.activeElement = other;
  f.guard(button)(); assert.equal(f.document.activeElement, other); assert.equal(f.listeners.size, 0);
  f.document.activeElement = button; const restore = f.guard(button); button.isConnected = false;
  f.document.activeElement = f.document.body; restore(); assert.equal(f.document.activeElement, f.document.body);
});
