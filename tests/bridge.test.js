// Tests for bridge.js, run against a fake Termius (React fibers + xterm buffer) in a vm.
//   node --test tests/
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', 'bridge.js'), 'utf8');
const plain = x => JSON.parse(JSON.stringify(x));

// ---- fake xterm -----------------------------------------------------------
const cell = (chars, width = 1) => ({ getChars: () => chars, getWidth: () => width });
const row = (cells, isWrapped = false) => ({
  isWrapped,
  length: cells.length,
  getCell: i => cells[i],
  translateToString: (trim, start = 0, end = cells.length) => {
    // like xterm: a width-0 cell (right half of a wide char) adds nothing; a width-1 empty cell is a space
    const s = cells.slice(start, end).map(c => (c.getWidth() === 0 ? '' : c.getChars() || ' ')).join('');
    return trim ? s.replace(/\s+$/, '') : s;
  },
});
const text = (s, wrapped = false, pad = 0) =>
  row([...s].map(c => cell(c)).concat(Array.from({ length: pad }, () => cell(' '))), wrapped);

function fakeTerminal(rows, cursorY = rows.length - 1, cursorX = 0) {
  const inputs = [];
  return {
    inputs,
    cols: 80,
    element: { id: 'el' },
    buffer: { active: { baseY: 0, cursorY, cursorX, length: rows.length, getLine: i => rows[i] } },
    input: d => inputs.push(d),
  };
}

// ---- fake Termius renderer --------------------------------------------------
function load({ terminals = {}, sessions = [], mounted = [], withPool = true, state = {}, hostsList = null, buttons = [] } = {}) {
  const mediatorMap = new Map(
    Object.entries(terminals).map(([id, t]) => [id, { terminalApi: { id, terminal: t } }]));
  const listFiber = hostsList ? { memoizedProps: null, stateNode: hostsList } : null;
  const poolFiber = { memoizedProps: { singletons: { mediatorPool: { mediatorMap } } }, sibling: listFiber };
  const storeFiber = {
    memoizedProps: {
      store: {
        getState: () => ({
          entities: { hosts: [], visibleIdentities: [], sshKeys: [], hardwareKeys: [], knownHosts: [], ...state.entities },
          terminal: { all: Object.fromEntries(sessions.map(s => [s.id, s])) },
        }),
      },
    },
    child: withPool ? poolFiber : listFiber,
  };
  const root = { '__reactContainer$x': { memoizedProps: null, child: storeFiber } };
  const mountedEls = new Set(mounted.map(id => terminals[id].element));
  const document = {
    getElementById: id => (id === 'root' ? root : null),
    querySelectorAll: sel => (sel === 'button' ? buttons : []),
    contains: el => mountedEls.has(el),
  };
  // Map is shared with the sandbox: bridge.js does `instanceof Map`, which fails across realms.
  const ctx = vm.createContext({ window: {}, document, Node: class {}, Map, console });
  vm.runInContext(SOURCE, ctx);
  return ctx.window.__kc;
}

const SESSION = (id, extra = {}) => ({ id, title: 'T-' + id, connectionProtocol: 'local',
  connectionEstablished: true, isActive: false, ...extra });

// ---- secrets never leave the renderer ----------------------------------------
test('hosts() returns whitelisted fields only', () => {
  const kc = load({ state: { entities: { hosts: [{
    label: 'web-1', address: '10.0.0.5', os_name: 'linux', group: { label: 'prod' },
    password: 'HOST-SECRET', privateKey: 'HOST-KEY',
    ssh_config: { port: 22, identity: { label: 'deploy', username: 'deploy',
      password: 'IDENT-SECRET', ssh_key: { label: 'k1', private_key: 'PEM-SECRET' } } },
    connectionPath: [{}, {}],
  }] } } });
  const out = JSON.stringify(kc.hosts());
  for (const s of ['HOST-SECRET', 'HOST-KEY', 'IDENT-SECRET', 'PEM-SECRET']) assert.ok(!out.includes(s), s);
  assert.deepEqual(plain(kc.hosts()[0]), { label: 'web-1', address: '10.0.0.5', port: 22, group: 'prod',
    username: 'deploy', identity: 'deploy', auth: 'key', jump_hosts: 1, os: 'linux' });
});

test('keychain() reports whether a password exists, never the password or key material', () => {
  const kc = load({ state: { entities: {
    visibleIdentities: [{ label: 'i1', username: 'u', password: 'IDENT-SECRET', ssh_key: { label: 'k1', private_key: 'PEM-SECRET' } }],
    sshKeys: [{ label: 'k1', private_key: 'PEM-SECRET', passphrase: 'PASS-SECRET' }],
    hardwareKeys: [{ label: 'yubi', pin: '1234' }],
    knownHosts: [{ hostnames: 'h', key: 'ssh-ed25519 AAAAKEYBODY' }],
  } } });
  const k = kc.keychain();
  const out = JSON.stringify(k);
  for (const s of ['IDENT-SECRET', 'PEM-SECRET', 'PASS-SECRET', '1234', 'AAAAKEYBODY']) assert.ok(!out.includes(s), s);
  assert.equal(k.identities[0].has_password, true);
  assert.equal(k.known_hosts[0].key_type, 'ssh-ed25519');
});

test('sessions() does not expose host credentials', () => {
  const t = fakeTerminal([text('$ ')]);
  const kc = load({ terminals: { a: t }, sessions: [SESSION('a', { host: { label: 'h', address: '192.0.2.10', password: 'SECRET' } })] });
  assert.ok(!JSON.stringify(kc.sessions()).includes('SECRET'));
});

// ---- finding terminals ------------------------------------------------------------
test('a tab that is not mounted in the DOM is still reachable through the mediator pool', () => {
  const t = fakeTerminal([text('hello')]);
  const kc = load({ terminals: { a: t }, sessions: [SESSION('a')], mounted: [] });
  const s = kc.sessions()[0];
  assert.deepEqual([s.rendered, s.mounted, s.via], [true, false, 'pool']);
  assert.deepEqual(plain(kc.read('a').lines), ['hello']);
  kc.input('a', 'ls');
  assert.deepEqual(t.inputs, ['ls']);
});

test('mounted tabs are reported as mounted', () => {
  const t = fakeTerminal([text('x')]);
  const kc = load({ terminals: { a: t }, sessions: [SESSION('a')], mounted: ['a'] });
  assert.equal(kc.sessions()[0].mounted, true);
});

test('unknown tab: read/scrollback/input all throw and nothing is typed', () => {
  const t = fakeTerminal([text('x')]);
  const kc = load({ terminals: { a: t }, sessions: [SESSION('a')] });
  for (const f of [() => kc.read('zzz'), () => kc.scrollback('zzz'), () => kc.input('zzz', 'rm')])
    assert.throws(f, /terminal not found/);
  assert.deepEqual(t.inputs, []);
});

test('input goes to the requested tab only', () => {
  const a = fakeTerminal([text('a')]), b = fakeTerminal([text('b')]);
  const kc = load({ terminals: { a, b }, sessions: [SESSION('a'), SESSION('b')] });
  kc.input('b', 'x');
  assert.deepEqual([a.inputs, b.inputs], [[], ['x']]);
});

test('a pool entry whose id does not match the requested id is not used', () => {
  const t = fakeTerminal([text('x')]);
  const kc = load({ terminals: { a: t }, sessions: [SESSION('a'), SESSION('b')] });
  // pool maps "a" -> terminalApi{id:"a"}; "b" is absent, so no cross-tab delivery
  assert.throws(() => kc.input('b', 'x'), /terminal not found/);
  assert.deepEqual(t.inputs, []);
});

test('no pool and nothing mounted: terminals are unreachable, not guessed', () => {
  const t = fakeTerminal([text('x')]);
  const kc = load({ terminals: { a: t }, sessions: [SESSION('a')], withPool: false });
  assert.equal(kc.sessions()[0].rendered, false);
  assert.throws(() => kc.input('a', 'x'), /terminal not found/);
});

// ---- reading rows --------------------------------------------------------------------
test('soft-wrapped rows are joined and keep real interior spaces', () => {
  const t = fakeTerminal([text('foo ', false), text('bar', true)]);
  const kc = load({ terminals: { a: t }, sessions: [SESSION('a')] });
  assert.deepEqual(plain(kc.read('a').lines), ['foo bar']);
});

test('the blank cell left when a wide char did not fit is dropped; the wide char is kept', () => {
  const r1 = row([cell('a'), cell('b'), cell('')]);                  // skipped cell at the row end
  const r2 = row([cell('中', 2), cell('', 0), cell('c')], true);     // wide char starts the next row
  const kc = load({ terminals: { a: fakeTerminal([r1, r2]) }, sessions: [SESSION('a')] });
  assert.deepEqual(plain(kc.read('a').lines), ['ab中c']);
});

test('the second half of a wide char (width 0, empty) is not mistaken for a skipped cell', () => {
  const r1 = row([cell('中', 2), cell('', 0)]);
  const r2 = row([cell('文', 2), cell('', 0)], true);
  const kc = load({ terminals: { a: fakeTerminal([r1, r2]) }, sessions: [SESSION('a')] });
  assert.deepEqual(plain(kc.read('a').lines), ['中文']);
});

test('read(id, from) starts at the given row and reports the cursor line', () => {
  const t = fakeTerminal([text('old'), text('$ ls'), text('out'), text('$ ')], 3);
  const kc = load({ terminals: { a: t }, sessions: [SESSION('a')] });
  const r = kc.read('a', 2);
  assert.deepEqual(plain(r.lines), ['out', '$']);   // trailing space trimmed
  assert.equal(r.prompt_row, 3);
  assert.equal(r.prompt_line, '$');
  assert.equal(r.cursor_row, 3);
});

test('read(id, from) never starts in the middle of a wrapped line', () => {
  const t = fakeTerminal([text('abc'), text('def', true), text('$ ')], 2);
  const kc = load({ terminals: { a: t }, sessions: [SESSION('a')] });
  assert.deepEqual(plain(kc.read('a', 1).lines), ['abcdef', '$']);
});

test('read() reports the text before the cursor, so a prompt can be told from output ending in %', () => {
  // "me@box % " with the cursor after the trailing space (col 9), padded to the row width
  const prompt = text('me@box % ', false, 20);
  const kc = load({ terminals: { a: fakeTerminal([prompt], 0, 9) }, sessions: [SESSION('a')] });
  const r = kc.read('a');
  assert.equal(r.before_cursor, 'me@box % ');
  assert.equal(r.cursor_x, 9);
  assert.equal(r.prompt_line, 'me@box %');      // trimmed; only before_cursor shows the space
});

test('read() before_cursor has no trailing space for a progress line', () => {
  const progress = text('Downloading... 45%', false, 20);
  const kc = load({ terminals: { a: fakeTerminal([progress], 0, 18) }, sessions: [SESSION('a')] });
  assert.equal(kc.read('a').before_cursor, 'Downloading... 45%');
});

test('before_cursor ignores text to the right of the cursor', () => {
  const line = text('me@box % ls', false, 5);
  const kc = load({ terminals: { a: fakeTerminal([line], 0, 9) }, sessions: [SESSION('a')] });
  assert.equal(kc.read('a').before_cursor, 'me@box % ');
});

// ---- connectHost --------------------------------------------------------------
let nextLocalId = 1;
const HOST = (label, extra = {}) => ({ local_id: nextLocalId++, label, address: '10.0.0.' + label.length, ssh_config: { port: 22 }, ...extra });
// The redux copy (state.entities.hosts) is plain data; the Hosts list holds the app's own
// Host instances. Model that: live copies are distinct objects with the same local_id.
function withList(hosts, liveHosts) {
  const calls = [];
  const live = liveHosts || hosts.map(h => ({ ...h, isLive: true }));
  const hostsList = { connectToHostsImpl: arg => calls.push(arg), props: { hosts: live } };
  return { calls, live, kc: load({ hostsList, state: { entities: { hosts } } }) };
}

test('connectHost opens the saved host in a tab of this window, never a new window', () => {
  const h = HOST('web-1', { identity_secret: 'PW-SECRET' });
  const { calls, live, kc } = withList([h, HOST('db-1')]);
  const r = plain(kc.connectHost('WEB-1', false));
  assert.equal(calls.length, 1);
  assert.equal(calls[0].hosts.length, 1);
  assert.equal(calls[0].hosts[0], live[0]); // the app's live instance, not the redux copy
  assert.equal(calls[0].newWindow, false);
  assert.equal(r.label, 'web-1');
  assert.ok(!JSON.stringify(r).includes('PW-SECRET'));
});

test('connectHost dryRun resolves the host and calls nothing', () => {
  const { calls, kc } = withList([HOST('web-1')]);
  assert.equal(kc.connectHost('web-1', true).dry_run, true);
  assert.equal(calls.length, 0);
});

test('connectHost refuses unknown, empty and duplicate labels without connecting', () => {
  const { calls, kc } = withList([HOST('web-1'), HOST('web-1'), HOST('db-1')]);
  assert.throws(() => kc.connectHost('10.0.0.9', false), /no saved host/);
  assert.throws(() => kc.connectHost('  ', false), /empty/);
  assert.throws(() => kc.connectHost('web-1', false), /2 saved hosts/);
  assert.equal(calls.length, 0);
});

test('connectHost refuses when the Hosts list has no live entry for the host', () => {
  const { calls, kc } = withList([HOST('web-1')], []);
  assert.throws(() => kc.connectHost('web-1', false), /0 live entries/);
  assert.throws(() => kc.connectHost('web-1', true), /0 live entries/);
  assert.equal(calls.length, 0);
});

test('connectHost fails clearly when the hosts list is not mounted', () => {
  const kc = load({ state: { entities: { hosts: [HOST('web-1')] } } });
  assert.throws(() => kc.connectHost('web-1', false), /hosts list not found/);
});


// ---- answerHostKey ------------------------------------------------------------
// A prompt = a container holding the message and three buttons, like Termius' DOM.
function prompt(address, { fp = 'YYDEmmEzEunsZXdgoxhAOVGGwCqfIEaLNmr1zsIhu+A', head = 'The authenticity of %A can not be established.' } = {}) {
  const pressed = [];
  const box = { textContent: '', parentElement: null, _b: [] };
  const mk = name => { const b = { textContent: name, parentElement: box, click: () => pressed.push(name) }; return b; };
  box._b = ['Close', 'Continue', 'Add and continue'].map(mk);
  box.querySelectorAll = () => box._b;
  box.textContent = `Are you sure you want to connect?${head.replace('%A', address)}RSA fingerprint is SHA256:${fp}Do you want to add it to the list of known hosts?CloseContinueAdd and continue`;
  return { box, pressed, buttons: box._b };
}

test('answerHostKey presses "Add and continue" for the prompt naming the address and returns the fingerprint', () => {
  const p = prompt('10.0.0.1');
  const kc = load({ buttons: p.buttons });
  const r = plain(kc.answerHostKey('10.0.0.1', 'add'));
  assert.deepEqual(p.pressed, ['Add and continue']);
  assert.equal(r.fingerprint, 'SHA256:YYDEmmEzEunsZXdgoxhAOVGGwCqfIEaLNmr1zsIhu+A');
});

test('answerHostKey "once" presses Continue, an unknown mode presses nothing', () => {
  const p = prompt('10.0.0.1');
  const kc = load({ buttons: p.buttons });
  kc.answerHostKey('10.0.0.1', 'once');
  assert.deepEqual(p.pressed, ['Continue']);
  assert.throws(() => kc.answerHostKey('10.0.0.1', 'always'), /unknown host-key mode/);
  assert.deepEqual(p.pressed, ['Continue']);
});

test('answerHostKey ignores a prompt for another address and returns null', () => {
  const p = prompt('10.0.0.9');
  const kc = load({ buttons: p.buttons });
  assert.equal(kc.answerHostKey('10.0.0.1', 'add'), null);
  assert.deepEqual(p.pressed, []);
});

test('answerHostKey never answers a changed-key warning', () => {
  const p = prompt('10.0.0.1', { head: 'WARNING: REMOTE HOST IDENTIFICATION FOR %A HAS CHANGED!' });
  const kc = load({ buttons: p.buttons });
  assert.equal(kc.answerHostKey('10.0.0.1', 'add'), null);
  assert.deepEqual(p.pressed, []);
});

test('answerHostKey refuses when two prompts name the same address', () => {
  const a = prompt('10.0.0.1'), b = prompt('10.0.0.1');
  const kc = load({ buttons: [...a.buttons, ...b.buttons] });
  assert.throws(() => kc.answerHostKey('10.0.0.1', 'add'), /2 host-key prompts/);
  assert.deepEqual([...a.pressed, ...b.pressed], []);
});

test('answerHostKey with only the other prompt present answers just that one', () => {
  const a = prompt('10.0.0.1'), b = prompt('10.0.0.2');
  const kc = load({ buttons: [...a.buttons, ...b.buttons] });
  kc.answerHostKey('10.0.0.2', 'add');
  assert.deepEqual(a.pressed, []);
  assert.deepEqual(b.pressed, ['Add and continue']);
});
