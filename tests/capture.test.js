// Real xterm parser and bounded buffer; only the Termius React container is simulated.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { Terminal } = require('@xterm/headless');
const SOURCE = fs.readFileSync(path.join(__dirname, '..', 'bridge.js'), 'utf8');
const write = (t, s) => new Promise(resolve => t.write(s, resolve));

function load(t) {
  const mediatorMap = new Map([['a', {terminalApi: {id: 'a', terminal: t}}]]);
  const root = {__reactContainer$test: {memoizedProps: {singletons: {mediatorPool: {mediatorMap}}}}};
  const ctx = vm.createContext({window: {}, Node: class {}, Map, console,
    document: {getElementById: () => root, contains: () => false, querySelectorAll: () => []}});
  vm.runInContext(SOURCE, ctx);
  return ctx.window.__kc;
}

test('marker follows scrollback trimming while command anchor survives', async t => {
  const terminal = new Terminal({cols: 40, rows: 3, scrollback: 3, allowProposedApi: true});
  t.after(() => terminal.dispose());
  await write(terminal, 'old1\r\nold2\r\nold3\r\nold4\r\n$ command');
  const bridge = load(terminal);
  assert.equal(typeof bridge.startCapture, 'function');
  bridge.startCapture('a', 'test-1');
  await write(terminal, '\r\nRESULT\r\n$ ');
  const r = bridge.readCapture('a', 'test-1');
  assert.equal(r.history_lost, false);
  assert.deepEqual(Array.from(r.lines), ['RESULT', '$ ']);
  bridge.stopCapture('a', 'test-1');
  assert.equal(terminal.markers.length, 0);
});

test('disposed anchor reports loss and still returns retained command output', async t => {
  const terminal = new Terminal({cols: 40, rows: 3, scrollback: 2, allowProposedApi: true});
  t.after(() => terminal.dispose());
  await write(terminal, '$ command');
  const bridge = load(terminal);
  assert.equal(typeof bridge.startCapture, 'function');
  bridge.startCapture('a', 'test-2');
  await write(terminal, '\r\na\r\nb\r\nc\r\nd\r\ne\r\nRESULT\r\n$ ');
  const r = bridge.readCapture('a', 'test-2');
  assert.equal(r.history_lost, true);
  assert.ok(r.lines.includes('RESULT'));
  assert.ok(!r.lines.includes('$ command'));
  bridge.stopCapture('a', 'test-2');
});

test('unsupported proposed API is refused without changing terminal options', t => {
  const terminal = new Terminal({allowProposedApi: false});
  t.after(() => terminal.dispose());
  const bridge = load(terminal);
  assert.equal(typeof bridge.startCapture, 'function');
  assert.throws(() => bridge.startCapture('a', 'no-api'), /marker|proposed/i);
  assert.equal(terminal.options.allowProposedApi, false);
});

test('capture rejects alternate screen and cross-session reads', async t => {
  const terminal = new Terminal({allowProposedApi: true});
  t.after(() => terminal.dispose());
  const bridge = load(terminal);
  assert.equal(typeof bridge.startCapture, 'function');
  bridge.startCapture('a', 'normal');
  assert.throws(() => bridge.readCapture('b', 'normal'), /capture/);
  await write(terminal, '\x1b[?1049h');
  assert.throws(() => bridge.readCapture('a', 'normal'), /alternate/);
  assert.throws(() => bridge.startCapture('a', 'alt'), /normal|alternate/);
  bridge.stopCapture('a', 'normal');
});
