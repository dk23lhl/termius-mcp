// Local adapter based on static inspection of Termius 9.38.1. Private APIs:
// missing/ambiguous shapes are refused. No credentials or arbitrary Redux data
// cross the bridge. Runtime compatibility must still be checked on the real app.
(() => {
  const bridge = window.__kc;
  const fibers = () => {
    const root = document.getElementById('root');
    const key = root && Object.keys(root).find(k => k.startsWith('__reactContainer'));
    if (!key) throw new Error('Termius React root unavailable');
    const result = [], seen = new WeakSet(), queue = [root[key]];
    for (let i = 0; i < queue.length && i < 60000; i++) {
      const f = queue[i];
      if (!f || typeof f !== 'object' || seen.has(f)) continue;
      seen.add(f); result.push(f);
      if (f.child) queue.push(f.child);
      if (f.sibling) queue.push(f.sibling);
    }
    return result;
  };
  const store = () => {
    for (const f of fibers()) {
      const p = f.memoizedProps;
      const s = p?.store || p?.value?.store;
      if (s && typeof s.getState === 'function' && typeof s.dispatch === 'function') return s;
    }
    throw new Error('compatible Termius store not found');
  };
  const pane = () => {
    const found = new Set();
    for (const f of fibers()) {
      const n = f.stateNode;
      if (n && typeof n.handleTransferFromLocalToLeft === 'function'
          && typeof n.handleTransferFromLocalToRight === 'function'
          && typeof n.handleTransfer === 'function') found.add(n);
    }
    if (found.size !== 1) throw new Error('open exactly one SFTP view in this Termius window');
    return [...found][0];
  };
  const dispatch = (s, type, payload) => s.dispatch({type, payload, meta: {scope: 'local'}});
  const requireSession = (s, id) => {
    if (!s.getState().terminal?.all?.[id]) throw new Error('terminal session not found');
  };
  const norm = s => String(s || '').replace(/\/+$/, '') || '/';
  const sessionEntries = p => ['Left', 'Right'].map(side => ({side, session: p['sftpSession' + side],
    host: p.state?.['host' + side]})).filter(e => e.session?.sessionId);
  const transferQueue = s => {
    // 9.38.1 reducers serialize the Map as [...map] after every add/remove.
    const rows = s.getState().transferStreams;
    if (!Array.isArray(rows) || rows.some(row => !Array.isArray(row) || row.length !== 2
        || typeof row[0] !== 'string' || !row[1] || typeof row[1] !== 'object')) {
      throw new Error('unsupported native SFTP interface: transfer queue shape');
    }
    const queue = new Map(rows);
    if (queue.size !== rows.length) throw new Error('unsupported native SFTP interface: duplicate task ID');
    return queue;
  };
  const jobs = new Map();
  const uploadTag = Symbol('termius-local-upload');
  const visibleJob = j => ({transfer_id: j.token, native_task_id: j.nativeId || null,
    status: j.status, finalized: j.finalized, session: j.session, local_path: j.localPath,
    remote_directory: j.remoteDirectory, filename: j.filename, size: j.size,
    progress: {...j.progress}, error: j.error || null, prior_errors: [...j.priorErrors]});
  const progressFields = ['percentage', 'transferred', 'remaining', 'length', 'speed', 'filesTransferred'];
  const errorText = e => String(Array.isArray(e) ? e.map(x => x?.error || 'transfer error').join('; ')
    : e?.message || e || 'transfer error').slice(0, 2000);

  bridge.activateSession = id => {
    const s = store(); requireSession(s, id);
    const data = s.getState();
    const tabs = (data.terminal.tabs || []).filter(t =>
      t.tabType === 'terminalTab' ? t.terminalId === id :
        t.tabType === 'workspaceTab' && data.workspace?.all?.[t.workspaceId]?.layout?.has(id));
    if (tabs.length !== 1) throw new Error('containing tab is missing or ambiguous; nothing switched');
    dispatch(s, 'ACTIVATE_TAB', tabs[0].id);
    dispatch(s, 'ACTIVATE_TERMINAL', id);
    return {requested: true, tab_id: tabs[0].id};
  };
  bridge.closeSession = id => {
    const s = store(); requireSession(s, id);
    dispatch(s, 'CLOSE_TERMINAL', id);
    return {requested: true};
  };
  bridge.sftpSessions = () => sessionEntries(pane()).map(e => ({
    id: e.session.sessionId, side: e.side.toLowerCase(),
    local: e.session.sessionId === 'local-session',
    label: typeof e.host === 'object' ? e.host?.label || null : e.host || null,
    address: typeof e.host === 'object' ? e.host?.address || null : null,
    directory: e.session.path?.path || null,
  }));

  bridge.startUpload = async request => {
    const {token, session, local_path, local_directory, filename, remote_directory, size} = request;
    if (!token || jobs.has(token)) throw new Error('unique transfer token required');
    if (!local_path?.startsWith('/') || !local_directory?.startsWith('/') || !remote_directory?.startsWith('/')
        || !filename || filename.includes('/') || /[\x00-\x1f\x7f-\x9f]/.test(filename)
        || !Number.isSafeInteger(size) || size < 0) throw new Error('invalid upload paths or file size');
    for (const j of jobs.values()) {
      if (!j.finalized && j.session === session && j.filename === filename
          && norm(j.remoteDirectory) === norm(remote_directory)) throw new Error('upload already pending; check its transfer ID');
    }
    if (jobs.size >= 100) {
      for (const [key, j] of jobs) if (j.finalized) { jobs.delete(key); break; }
      if (jobs.size >= 100) throw new Error('too many active uploads');
    }
    const p = pane(), s = store(), entries = sessionEntries(p);
    const hits = entries.filter(e => e.session.sessionId === session && session !== 'local-session');
    if (hits.length !== 1) throw new Error('remote SFTP session missing or ambiguous');
    const target = hits[0].session;
    if (norm(target.path?.path) !== norm(remote_directory)) throw new Error('remote directory differs from the SFTP pane; navigate there first');
    const locals = entries.filter(e => e.session.sessionId === 'local-session');
    if (locals.length !== 1) throw new Error('keep one SFTP pane connected to Local for native upload');
    if (typeof target.clone !== 'function' || typeof locals[0].session.clone !== 'function'
        || typeof s.subscribe !== 'function') {
      throw new Error('unsupported native SFTP interface');
    }
    transferQueue(s); // Validate the observed queue shape before any transfer preparation.
    // Clone session/path state so clicking another host/directory cannot retarget
    // an in-flight upload. Native clients and the UI conflict resolver are used.
    const source = locals[0].session.clone(), destination = target.clone();
    source.path = new locals[0].session.path.constructor(local_directory);
    destination.path = new target.path.constructor(remote_directory);
    // Native createTransferStream clones its source. Give only THIS request's
    // private clone a clone method that preserves a non-enumerable job tag.
    // Shared clients/paths are not identities: a human may upload the same file.
    const cloneSource = source.clone.bind(source);
    source.clone = () => {
      const copy = cloneSource();
      Object.defineProperty(copy, uploadTag, {value: token});
      return copy;
    };
    const item = await source.client.getItemSFTP(local_path, filename);
    if (item.isDirectory || item.isLink || item.isSymbolicLink || item.attrs?.size !== size) {
      throw new Error('local source is not the expected regular file or has changed');
    }
    // Repeat validation after asynchronous preparation and before starting a transfer.
    if (!sessionEntries(p).some(e => e.session === target && norm(e.session.path?.path) === norm(remote_directory))) {
      throw new Error('SFTP target changed during preparation; nothing uploaded');
    }
    for (const old of jobs.values()) {
      if (!old.finalized && old.session === session && old.filename === filename
          && norm(old.remoteDirectory) === norm(remote_directory)) throw new Error('upload already pending');
    }
    const j = {token, session, filename, size, localPath: local_path, remoteDirectory: remote_directory,
      status: 'preparing', finalized: false, progress: {}, nativeId: null, error: null, priorErrors: []};
    jobs.set(token, j);
    const existing = new Set(transferQueue(s).keys());
    let unsubscribe = () => {}, listeners = [], tracked = null;
    const finish = (status, error) => {
      if (j.finalized) return;
      j.status = status; j.error = error || null; j.finalized = true;
      unsubscribe();
      if (tracked) for (const [event, fn] of listeners) tracked.removeListener(event, fn);
      listeners = [];
    };
    const scan = () => {
      if (j.nativeId || j.finalized) return;
      let streams;
      try { streams = transferQueue(s); }
      catch (_) { return finish('unknown', 'native transfer queue unavailable; do not retry'); }
      for (const [id, stream] of streams) {
        if (existing.has(id) || stream.source?.[uploadTag] !== token || stream.destination?.sessionId !== session
            || stream.destination?.client !== destination.client
            || norm(stream.destinationPath || stream.destination?.path?.path) !== norm(remote_directory)
            || norm(stream.sourcePath || stream.source?.path?.path) !== norm(local_directory)) continue;
        const files = stream.data?.files;
        if (!Array.isArray(files) || files.length !== 1 || files[0].source?.origin?.filename !== filename
            || files[0].source?.origin?.attrs?.size !== size) continue;
        if (typeof stream.on !== 'function' || typeof stream.removeListener !== 'function') {
          return finish('unknown', 'native transfer events unavailable; do not retry');
        }
        j.nativeId = String(id); j.status = 'running'; tracked = stream;
        listeners = [
          ['progress', data => { for (const key of progressFields) if (Number.isFinite(data?.[key])) j.progress[key] = data[key]; }],
          // Termius retains a failed stream and can restart it from its Retry UI.
          // Keep observation and duplicate protection until end/close/skip.
          ['error', e => {
            j.status = 'failed'; j.error = errorText(e);
            j.priorErrors.push(j.error);
            if (j.priorErrors.length > 20) j.priorErrors.shift();
          }],
          ['skip', () => finish('incomplete', 'files were skipped')],
          ['close', () => finish('cancelled', 'native transfer closed without confirmed completion')],
          ['end', () => {
            if (stream.deferredFiles?.length || stream.filesTransferred !== 1 || stream.filesCount !== 1) {
              finish('incomplete', 'native end did not confirm all requested files');
            } else finish('completed');
          }],
        ];
        for (const [event, fn] of listeners) stream.on(event, fn);
        break;
      }
    };
    unsubscribe = s.subscribe(scan);
    // Native UI may wait for an overwrite/error dialog. Return the job immediately;
    // never press its buttons or silently start another transfer on timeout.
    Promise.resolve().then(() => p.handleTransfer({source, destination, items: [item],
      getSessionId: () => session,
      throttleRefresh: () => { const fn = p['throttleRefresh' + hits[0].side]; if (typeof fn === 'function') fn(); },
    })).then(() => {
      scan();
      if (!j.nativeId && !j.finalized) finish('unknown', 'native UI returned without an observable transfer; inspect its dialog, do not retry');
    }).catch(e => finish('failed', errorText(e)));
    return visibleJob(j);
  };
  bridge.transferStatus = token => {
    const j = jobs.get(token);
    if (!j) throw new Error('transfer record unavailable (renderer reloaded or ID unknown); do not retry automatically');
    return visibleJob(j);
  };
  return true;
})()
