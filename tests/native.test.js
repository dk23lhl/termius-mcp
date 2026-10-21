const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {EventEmitter} = require('node:events');
const file = path.join(__dirname, '..', 'native.js');
const flush = () => new Promise(resolve => setImmediate(resolve));

function fixture() {
  class NativePath { constructor(path) { this.path = path; } }
  const queueSet = (queue, id, stream) => [...new Map(queue).set(id, stream)];
  const queueDelete = (queue, id) => {
    const next = new Map(queue);
    next.delete(id);
    return [...next];
  };
  const makeSession = (id, pathname) => ({sessionId: id, path: new NativePath(pathname),
    client: {getItemSFTP: async (path, filename) => ({filename, isDirectory: false, attrs: {size: 3}, path})},
    clone() { return {...this}; }});
  const local = makeSession('local-session', '/Users/test');
  local.clone = function cloneLocal() {
    const copy = makeSession('local-session', this.path.path);
    copy.clone = cloneLocal;
    return copy; // Actual nc.clone constructs a new local filesystem client.
  };
  const remote = makeSession('remote-a', '/upload');
  const state = {transferStreams: [], terminal: {all: {
    a: {id: 'a', isActive: false}, b: {id: 'b', isActive: true}},
    tabs: [{id: 'tab-a', tabType: 'terminalTab', terminalId: 'a'}]}};
  const observers = new Set();
  const setTransferStreams = streams => {
    state.transferStreams = streams;
    for (const fn of observers) fn();
  };
  const actions = [];
  const store = {getState: () => state, subscribe(fn) { observers.add(fn); return () => observers.delete(fn); },
    dispatch(action) { actions.push(action);
      if (action.type === 'ACTIVATE_TERMINAL') state.terminal.all[action.payload].isActive = true;
      if (action.type === 'ACTIVATE_TAB') state.terminal.activeTabId = action.payload;
      if (action.type === 'CLOSE_TERMINAL') delete state.terminal.all[action.payload];
    }};
  let stream, uploads = 0;
  const pane = {sftpSessionLeft: local, sftpSessionRight: remote,
    state: {hostLeft: 'localhost', hostRight: {label: 'server-a', address: '192.0.2.1', password: 'SECRET'}},
    handleTransferFromLocalToLeft() {}, handleTransferFromLocalToRight() {},
    async handleTransfer({source, destination, items}) {
      uploads++;
      stream = new EventEmitter();
      Object.assign(stream, {source: source.clone(), destination: destination.clone(), data: {files: [{source: {path: items[0].filename, origin: items[0]}}]},
        filesCount: 1, filesTransferred: 0, deferredFiles: []});
      if (pane.beforeTransfer) pane.beforeTransfer(stream, state, observers, setTransferStreams);
      setTransferStreams(queueSet(state.transferStreams, 'native-1', stream));
    }};
  const root = {__reactContainer$test: {memoizedProps: {store}, child: {stateNode: pane}}};
  const context = vm.createContext({window: {__kc: {}}, document: {getElementById: () => root}, Node: class {}, Map, console});
  if (fs.existsSync(file)) vm.runInContext(fs.readFileSync(file, 'utf8'), context);
  return {bridge: context.window.__kc, state, pane, actions, observers, stream: () => stream,
    setTransferStreams, queueSet, queueDelete, uploads: () => uploads};
}
const request = {token: 'job-1', session: 'remote-a', local_path: '/Users/test/a.txt',
  local_directory: '/Users/test', filename: 'a.txt', remote_directory: '/upload', size: 3};

test('native upload tracks the exact queue ID and only explicit end means completed', async () => {
  const f = fixture();
  assert.equal(typeof f.bridge.startUpload, 'function');
  await f.bridge.startUpload(request); await flush();
  f.stream().emit('progress', {percentage: 100, transferred: 3, length: 3, filesTransferred: 1});
  assert.equal(f.bridge.transferStatus('job-1').status, 'running');
  f.setTransferStreams(f.queueDelete(f.state.transferStreams, 'native-1'));
  assert.equal(f.bridge.transferStatus('job-1').status, 'running');
  f.stream().filesTransferred = 1;
  f.stream().emit('end');
  const result = f.bridge.transferStatus('job-1');
  assert.equal(result.status, 'completed');
  assert.equal(result.native_task_id, 'native-1');
  assert.ok(!JSON.stringify(result).includes('SECRET'));
  assert.equal(f.observers.size, 0);
});

test('close and errors are not upload success, even at 100 percent', async () => {
  for (const event of ['close', 'error', 'skip']) {
    const f = fixture();
    assert.equal(typeof f.bridge.startUpload, 'function');
    await f.bridge.startUpload(request); await flush();
    f.stream().emit('progress', {percentage: 100});
    f.stream().emit(event, [{error: 'disk full'}]);
    assert.notEqual(f.bridge.transferStatus('job-1').status, 'completed');
    assert.ok(['cancelled', 'failed', 'incomplete'].includes(f.bridge.transferStatus('job-1').status));
  }
});

test('upload validates target directory and refuses duplicate active jobs', async () => {
  const f = fixture();
  assert.equal(typeof f.bridge.startUpload, 'function');
  await assert.rejects(() => f.bridge.startUpload({...request, remote_directory: '/wrong'}), /directory/);
  assert.equal(f.uploads(), 0);
  await f.bridge.startUpload(request); await flush();
  await assert.rejects(() => f.bridge.startUpload({...request, token: 'job-2'}), /already/);
  assert.equal(f.uploads(), 1);
});

test('malformed native transfer queue is rejected before upload', async () => {
  const f = fixture();
  f.setTransferStreams([['malformed-entry']]);
  await assert.rejects(() => f.bridge.startUpload(request));
  assert.equal(f.uploads(), 0);
});

test('session activation uses local action and selects the containing tab', () => {
  const f = fixture();
  assert.equal(typeof f.bridge.activateSession, 'function');
  f.bridge.activateSession('a');
  assert.equal(f.state.terminal.activeTabId, 'tab-a');
  assert.equal(f.state.terminal.all.a.isActive, true);
  assert.ok(f.actions.every(a => a.meta.scope === 'local'));
});

test('unknown close target has no side effect', () => {
  const f = fixture();
  assert.equal(typeof f.bridge.closeSession, 'function');
  assert.throws(() => f.bridge.closeSession('missing'), /session/);
  assert.equal(f.actions.length, 0);
  f.bridge.closeSession('a');
  assert.equal(f.state.terminal.all.a, undefined);
});

test('successful end with skipped or untransferred files stays incomplete', async () => {
  const f = fixture();
  await f.bridge.startUpload(request); await flush();
  f.stream().filesTransferred = 0;
  f.stream().emit('end');
  assert.equal(f.bridge.transferStatus('job-1').status, 'incomplete');
});

test('upload captures destination before the human changes the pane', async () => {
  const f = fixture();
  await f.bridge.startUpload(request); await flush();
  f.pane.sftpSessionRight.path.path = '/other';
  assert.equal(f.stream().destination.path.path, '/upload');
});

test('simultaneous human transfer with identical paths is not attributed to this request', async () => {
  const f = fixture();
  f.pane.beforeTransfer = (stream, state, observers, setTransferStreams) => {
    const other = new EventEmitter();
    // Native pane transfers can share the same clients AND all visible metadata.
    Object.assign(other, stream, {source: {...stream.source}});
    setTransferStreams([...new Map(state.transferStreams).set('unrelated', other)]);
  };
  await f.bridge.startUpload(request); await flush();
  assert.equal(f.bridge.transferStatus('job-1').native_task_id, 'native-1');
});

test('native Retry retains the task ID, updates completion and blocks another upload', async () => {
  const f = fixture();
  await f.bridge.startUpload(request); await flush();
  f.stream().emit('error', [{error: 'temporary failure'}]);
  assert.equal(f.bridge.transferStatus('job-1').status, 'failed');
  await assert.rejects(() => f.bridge.startUpload({...request, token: 'job-2'}), /already/);
  f.stream().filesTransferred = 1;
  f.stream().emit('end');
  assert.equal(f.bridge.transferStatus('job-1').status, 'completed');
  assert.equal(f.bridge.transferStatus('job-1').native_task_id, 'native-1');
  assert.ok(f.bridge.transferStatus('job-1').prior_errors.includes('temporary failure'));
});
