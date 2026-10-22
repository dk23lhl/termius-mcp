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

// Server order: capture at the prompt, type, re-anchor once the echo settles, Enter.
const visible = r => Array.from(r.lines).map(s => s.trimEnd()).filter(Boolean);
const OUTPUT = '\r\nOUT1\r\nOUT2\r\n$ ';
async function command(cols, cmd, resizes = [],
                       {enterBeforeEcho = false, fill = 0, output = OUTPUT, after, beforeAgain} = {}) {
  const terminal = new Terminal({cols, rows: 6, scrollback: 100, allowProposedApi: true});
  const bridge = load(terminal);
  // 120-cell history lines: one row at 147 columns, two rows at 70. With 200 of them,
  // narrowing to 70 rewraps the history past the 100-row scrollback limit.
  for (let i = 0; i < fill; i++) await write(terminal, `H${i} `.padEnd(120, 'z') + '\r\n');
  await write(terminal, 'OLD-HISTORY\r\n$ ');
  bridge.startCapture('a', 'cmd');
  if (!enterBeforeEcho) await write(terminal, cmd);
  bridge.resetCapture('a', 'cmd');
  if (enterBeforeEcho) await write(terminal, cmd);     // remote echo arrives late
  await write(terminal, output);
  if (after) await write(terminal, after);
  for (const c of resizes) { terminal.resize(c, 6); await write(terminal, ''); }
  const r = bridge.readCapture('a', 'cmd');
  if (beforeAgain) await write(terminal, beforeAgain);
  const again = bridge.readCapture('a', 'cmd');         // after re-pinning, no rescan
  bridge.stopCapture('a', 'cmd');
  terminal.dispose();
  return {r, again};
}

const LONG = 'echo ' + 'x'.repeat(30);      // 32 cells with the prompt: wraps at 20 columns
for (const [name, cols, cmd, resizes, opts] of [
  ['wrapped echo, no resize', 20, LONG, []],
  ['wrapped echo, wider', 20, LONG, [60]],
  ['wrapped echo, narrower', 20, LONG, [10]],
  ['one-row echo, narrower', 30, 'echo hello-world', [10]],
  ['one-row echo, wider', 30, 'echo hello-world', [60]],
  // Found live on Termius: when narrowing rewraps the history past the scrollback limit,
  // xterm trims rows and leaves the marker on an older row; widening again disposes it.
  ['narrowing past the scrollback limit', 147, 'echo hello-world', [70], {fill: 200}],
  ['narrowing past the scrollback limit, then back', 147, 'echo hello-world', [70, 147], {fill: 200}],
]) {
  test(`resize keeps the anchor and excludes history and echo: ${name}`, async () => {
    const {r, again} = await command(cols, cmd, resizes, opts);
    for (const x of [r, again]) {
      assert.deepEqual(visible(x), ['OUT1', 'OUT2', '$']);
      assert.equal(x.history_lost, false);
      assert.equal(x.anchor_lost, false);
      assert.equal(x.resized, resizes.length > 0);
    }
  });
}

test('echo arriving after the anchor is still excluded when it wraps', async () => {
  const {r} = await command(20, LONG, [], {enterBeforeEcho: true});
  assert.deepEqual(visible(r), ['OUT1', 'OUT2', '$']);
  assert.equal(r.history_lost, false);
});

test('an anchor that cannot be found after a resize returns the screen, not history', async () => {
  // A transient prompt rewrites the command line after it ran; then the terminal resizes.
  // Later output printing the command line again must not be taken for the anchor.
  const {r, again} = await command(147, 'echo hello-world', [70], {fill: 200,
    after: '\x1b[3A\r\x1b[2K> rewritten\x1b[3B', beforeAgain: '\r\n$ echo hello-world\r\nLATE\r\n$ '});
  for (const x of [r, again]) {
    assert.equal(x.anchor_lost, true);
    assert.equal(x.anchor_reason, 'not-found');
    // Only the 6-row screen comes back: at most the newest history line is still on it.
    assert.ok(visible(x).length <= 6);
    assert.ok(visible(x).filter(l => /^H\d+ /.test(l)).every(l => l.startsWith('H199 ')));
  }
  assert.ok(visible(r).includes('OUT2'));
});

// Output that prints the command line again (cat of a transcript, a log of commands).
const REPEAT = '\r\nFIRST-IMPORTANT-RESULT\r\n$ cat log\r\nLATER\r\n$ ';

test('a resize reports ambiguity even when the marker still points at matching text', async () => {
  const {r, again} = await command(40, 'cat log', [30], {output: REPEAT});
  for (const x of [r, again]) {
    // An ambiguous boundary returns the current six-row screen. It may contain
    // earlier text; anchor_lost makes that limitation explicit to the caller.
    assert.deepEqual(visible(x), ['OLD-HISTORY', '$ cat log',
      'FIRST-IMPORTANT-RESULT', '$ cat log', 'LATER', '$']);
    assert.equal(x.anchor_lost, true);
    assert.equal(x.anchor_reason, 'ambiguous');
  }
});

test('a resized marker landing on identical older history is never trusted', async t => {
  const terminal = new Terminal({cols: 147, rows: 6, scrollback: 100, allowProposedApi: true});
  t.after(() => terminal.dispose());
  const bridge = load(terminal), cmd = 'echo ' + 'x'.repeat(113);
  // Every old command has the same text as the current anchor. Reflow can move
  // the marker to one of those rows without changing the text beneath it.
  for (let i = 0; i < 200; i++) await write(terminal, '$ ' + cmd + '\r\n');
  await write(terminal, '$ ');
  bridge.startCapture('a', 'old-copy');
  await write(terminal, cmd);
  bridge.resetCapture('a', 'old-copy');
  await write(terminal, '\r\nRESULT\r\n$ ');
  assert.deepEqual(visible(bridge.readCapture('a', 'old-copy')), ['RESULT', '$']);
  terminal.resize(70, 6);
  for (let i = 0; i < 2; i++) {
    const r = bridge.readCapture('a', 'old-copy');
    assert.equal(r.anchor_lost, true);
    assert.equal(r.anchor_reason, 'ambiguous');
    assert.equal(r.history_lost, false);
    assert.ok(visible(r).length <= terminal.rows);
    assert.ok(r.lines.includes('RESULT'));
  }
  bridge.stopCapture('a', 'old-copy');
});

test('a repeated command line with a misplaced marker is flagged, never guessed', async () => {
  // The marker no longer sits on the command line, and two lines match its text:
  // taking the later one used to drop FIRST-IMPORTANT-RESULT without any warning.
  const {r, again} = await command(147, 'cat log', [70], {fill: 200, output: REPEAT});
  for (const x of [r, again]) {
    assert.equal(x.anchor_lost, true);
    assert.equal(x.anchor_reason, 'ambiguous');
    assert.ok(visible(x).length <= 6);
  }
});

test('in a full scrollback a wrapped echo can leave the absolute cursor where it was', async () => {
  // Premise of the server's echo check: cursor coordinates alone do not prove an echo.
  const terminal = new Terminal({cols: 20, rows: 6, scrollback: 10, allowProposedApi: true});
  const bridge = load(terminal);
  for (let i = 0; i < 30; i++) await write(terminal, `h${i}\r\n`);
  await write(terminal, '$ ');
  const before = bridge.read('a');
  await write(terminal, 'x'.repeat(40));
  const after = bridge.read('a');
  terminal.dispose();
  assert.deepEqual([after.cursor_row, after.cursor_x], [before.cursor_row, before.cursor_x]);
  assert.notEqual(after.prompt_line, before.prompt_line);
});

// Termius' xterm reports parsed writes; the headless one does not, so these tests add
// the event, fired after each write is parsed.
function withWriteEvents(terminal) {
  const listeners = new Set();
  terminal.onWriteParsed = fn => { listeners.add(fn); return {dispose: () => listeners.delete(fn)}; };
  return s => new Promise(r => terminal.write(s, () => { for (const fn of listeners) fn(); r(); }));
}

for (const writeEvents of [false, true]) {
  test(`changes in omitted output advance its revision (write events: ${writeEvents})`, async t => {
    // A tall screen allows cursor-addressed progress updates in the omitted
    // middle while both transferred ends and the final cursor stay unchanged.
    const terminal = new Terminal({cols: 40, rows: 400, scrollback: 10, allowProposedApi: true});
    t.after(() => terminal.dispose());
    const w = writeEvents ? withWriteEvents(terminal) : s => write(terminal, s);
    const bridge = load(terminal);
    await w('$ cmd');
    bridge.startCapture('a', 'middle');
    await w('\r\n' + Array.from({length: 300}, (_, i) => `L${i}`).join('\r\n') + '\r\n$ ');
    const first = bridge.readCapture('a', 'middle', null, 50, 151);
    assert.equal(typeof first.output_revision, 'number');
    await w('\x1b7\x1b[102;1H\x1b[2KUPDATED\x1b8');
    const changed = bridge.readCapture('a', 'middle', first.sig, 50, 151);
    assert.deepEqual(Array.from(changed.lines), Array.from(first.lines));
    assert.equal(changed.omitted, first.omitted);
    assert.equal(changed.cursor_row, first.cursor_row);
    assert.equal(changed.cursor_x, first.cursor_x);
    assert.ok(changed.output_revision > first.output_revision);
    // Parser activity alone must not lengthen the existing quiet windows.
    await w('');
    const unchanged = bridge.readCapture('a', 'middle', null, 50, 151);
    assert.equal(unchanged.output_revision, changed.output_revision);
    bridge.stopCapture('a', 'middle');
  });
}

test('with a write counter, an unchanged terminal answers a known sig with "same" only', async t => {
  const terminal = new Terminal({cols: 40, rows: 6, scrollback: 100, allowProposedApi: true});
  t.after(() => terminal.dispose());
  const w = withWriteEvents(terminal), bridge = load(terminal);
  await w('$ ');
  bridge.startCapture('a', 's');
  await w('cmd');
  bridge.resetCapture('a', 's');
  await w('\r\nOUT\r\n$ ');
  const first = bridge.readCapture('a', 's', null, 50, 151);
  assert.equal(typeof first.sig, 'string');
  assert.deepEqual(visible(first), ['OUT', '$']);
  const quiet = bridge.readCapture('a', 's', first.sig, 50, 151);
  assert.equal(quiet.same, true);
  assert.equal(quiet.lines, undefined);
  await w('');                                    // a parsed write that changes no text
  const written = bridge.readCapture('a', 's', first.sig, 50, 151);
  assert.notEqual(written.same, true);
  terminal.resize(30, 6);
  const resized = bridge.readCapture('a', 's', written.sig, 50, 151);
  assert.notEqual(resized.same, true);
  assert.equal(resized.resized, true);
  bridge.stopCapture('a', 's');
});

test('without a write counter every read is a full read', async t => {
  const terminal = new Terminal({cols: 40, rows: 6, scrollback: 100, allowProposedApi: true});
  t.after(() => terminal.dispose());
  const bridge = load(terminal);
  await write(terminal, '$ ');
  bridge.startCapture('a', 'n');
  const first = bridge.readCapture('a', 'n', null, 50, 151);
  assert.equal(first.sig, null);
  const again = bridge.readCapture('a', 'n', first.sig, 50, 151);
  assert.notEqual(again.same, true);
  assert.ok(Array.isArray(Array.from(again.lines)));
  bridge.stopCapture('a', 'n');
});

test('long output transfers only the head and tail that will be shown', async t => {
  const terminal = new Terminal({cols: 40, rows: 6, scrollback: 1000, allowProposedApi: true});
  t.after(() => terminal.dispose());
  const bridge = load(terminal);
  await write(terminal, '$ ');
  bridge.startCapture('a', 'l');
  await write(terminal, 'seq');
  bridge.resetCapture('a', 'l');
  await write(terminal, '\r\n' + Array.from({length: 300}, (_, i) => `L${i}`).join('\r\n') + '\r\n$ ');
  const r = bridge.readCapture('a', 'l', null, 50, 151);
  const lines = Array.from(r.lines).map(s => s.trimEnd());
  assert.equal(r.head, 50);
  assert.equal(r.omitted, 100);                  // 301 lines: L50 .. L149 stay behind
  assert.equal(lines.length, 201);
  assert.deepEqual([lines[0], lines[49], lines[50], lines[199], lines[200]], ['L0', 'L49', 'L150', 'L299', '$']);
  const all = bridge.readCapture('a', 'l');
  assert.equal(all.omitted, 0);
  assert.equal(Array.from(all.lines).length, 301);
  bridge.stopCapture('a', 'l');
});

test('a stale wrapped row just past the buffer end is not read', () => {
  // Seen live: before the newline arrived, a capture read returned the command line,
  // because the row at index `length` (left over in the circular buffer) was flagged
  // as wrapped and the read walked back onto the command line.
  const row = (text, isWrapped) => ({isWrapped, length: 40, translateToString: () => text,
    getCell: () => null});
  const rows = [row('old', false), row('$ sleep 2', false)], stale = row('STALE', true);
  const terminal = {cols: 40, buffer: {active: {type: 'normal', baseY: 0, cursorY: 1, cursorX: 9,
    length: 2, getLine: i => (i < 2 ? rows[i] : i === 2 ? stale : undefined)}}};
  const bridge = load(terminal);
  assert.deepEqual(Array.from(bridge.read('a', 2).lines), []);
  assert.deepEqual(Array.from(bridge.read('a', 1).lines), ['$ sleep 2']);
});
