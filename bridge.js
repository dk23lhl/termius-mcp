// Injected into the Termius primary renderer. Installs window.__kc.
// Every function returns WHITELISTED fields only: the Redux entities hold
// decrypted secrets (identity.password, private keys), so nothing is ever
// passed through wholesale.
(() => {

  const findStore = () => {
    const root = document.getElementById('root');
    const key = root && Object.keys(root).find(k => k.startsWith('__reactContainer'));
    if (!key) return null;
    const seen = new WeakSet();
    let queue = [root[key]], depth = 0;
    while (queue.length && depth < 80) {
      const next = [];
      for (const f of queue) {
        if (!f || typeof f !== 'object' || seen.has(f)) continue;
        seen.add(f);
        const p = f.memoizedProps;
        try {
          if (p && p.store && typeof p.store.getState === 'function') return p.store;
          if (p && p.value && p.value.store && typeof p.value.store.getState === 'function') return p.value.store;
        } catch (e) {}
        if (f.child) next.push(f.child);
        if (f.sibling) next.push(f.sibling);
      }
      queue = next; depth++;
    }
    return null;
  };

  // Locate the terminalApi object (owns the xterm Terminal) behind one .xterm element.
  const apiFromElement = el => {
    const seeds = [];
    let node = el;
    for (let up = 0; node && up < 10; up++, node = node.parentElement)
      for (const k of Object.keys(node)) if (k.startsWith('__react')) seeds.push(node[k]);
    const seen = new WeakSet();
    let queue = seeds, depth = 0;
    while (queue.length && depth < 12) {
      const next = [];
      for (const obj of queue) {
        if (!obj || typeof obj !== 'object' || obj instanceof Node || seen.has(obj)) continue;
        seen.add(obj);
        try {
          if (obj.terminal && obj.terminal.buffer && typeof obj.terminal.cols === 'number' && obj.id) return obj;
        } catch (e) {}
        let keys; try { keys = Object.keys(obj); } catch (e) { continue; }
        if (keys.length > 300) continue;
        for (const k of keys) {
          let v; try { v = obj[k]; } catch (e) { continue; }
          if (v && typeof v === 'object') next.push(v);
        }
      }
      queue = next; depth++;
    }
    return null;
  };

  // Termius' mediator pool holds one mediator per live terminal, keyed by session
  // id, whether or not its tab is mounted in the DOM. It is passed down as
  // props.singletons.mediatorPool a few fiber levels below the root.
  let pool = null;
  const findPool = () => {
    if (pool && pool.mediatorMap instanceof Map) return pool;
    const root = document.getElementById('root');
    const key = root && Object.keys(root).find(k => k.startsWith('__reactContainer'));
    if (!key) return null;
    const queue = [root[key]];
    for (let visited = 0; queue.length && visited < 20000; visited++) {
      const f = queue.shift();
      if (!f) continue;
      try {
        const mp = f.memoizedProps && f.memoizedProps.singletons
          && f.memoizedProps.singletons.mediatorPool;
        if (mp && mp.mediatorMap instanceof Map) return (pool = mp);
      } catch (e) {}
      if (f.child) queue.push(f.child);
      if (f.sibling) queue.push(f.sibling);
    }
    return null;
  };

  const usable = a => !!(a && a.terminal && a.terminal.buffer && typeof a.terminal.cols === 'number');

  const apiFromPool = id => {
    const p = findPool();
    if (!p) return null;
    let m = null;
    try { m = p.mediatorMap.get(id) || null; } catch (e) {}
    const a = m && m.terminalApi;
    return usable(a) && a.id === id ? a : null;
  };

  // Pool first (covers tabs that are not mounted); DOM scan as the fallback for
  // a Termius build where the pool is not where we expect it.
  const apis = new Map();
  const api = id => {
    const fromPool = apiFromPool(id);
    if (fromPool) return fromPool;
    const cached = apis.get(id);
    if (cached && cached.terminal && document.contains(cached.terminal.element)) return cached;
    for (const el of document.querySelectorAll('.xterm')) {
      const a = apiFromElement(el);
      if (a) apis.set(a.id, a);
    }
    return apis.get(id) || null;
  };

  let store = null;
  const state = () => {
    if (!store) store = findStore();
    if (!store) throw new Error('Termius state store not found');
    return store.getState();
  };

  // Join soft-wrapped rows back into one line. A row that continues on the next
  // keeps its trailing spaces (they are real text), except the single blank cell
  // xterm leaves when a double-width char did not fit at the end of the row.
  const logical = (b, start, end) => {
    const out = [];
    for (let i = start; i < end; i++) {
      const ln = b.getLine(i);
      if (!ln) { out.push(''); continue; }
      const next = i + 1 < end ? b.getLine(i + 1) : null;
      const cont = !!(next && next.isWrapped);
      let txt = ln.translateToString(!cont);
      const last = cont && ln.length ? ln.getCell(ln.length - 1) : null;
      // width 1 + no chars = the skipped cell; width 0 is the right half of a wide char.
      if (last && last.getWidth() === 1 && last.getChars() === ''
          && next.getCell(0) && next.getCell(0).getWidth() === 2) txt = txt.slice(0, -1);
      if (ln.isWrapped && out.length) out[out.length - 1] += txt;
      else out.push(txt);
    }
    return out;
  };

  const identityOf = cfg => (cfg && cfg.identity) || null;

  // Markers move with xterm's bounded scrollback. A disposed marker means earlier
  // output may be lost; returning retained text with that fact is better than an
  // empty read from an obsolete absolute row. Never enable proposed APIs ourselves.
  const captures = new Map();
  const releaseCapture = c => { c.marker.dispose(); if (c.resize) c.resize.dispose(); };
  const capture = (id, token) => {
    const c = captures.get(token);
    if (!c || c.id !== id) throw new Error('capture not found for this session');
    if (api(id)?.terminal !== c.terminal) throw new Error('capture terminal was replaced; do not retry the command');
    if (c.terminal.buffer.active.type !== 'normal') throw new Error('alternate screen active; command state unknown');
    return c;
  };

  // The Hosts list component owns the code path behind "double-click a host":
  // connectToHostsImpl({hosts, protocol, newWindow, quick, connectionOrigin}) turns each
  // saved host into a connection and hands it to the app, which opens a tab. Calling
  // the live instance means the app's own logic picks identity, jump hosts and keys;
  // nothing secret is read or passed by us.
  const findHostsList = () => {
    const root = document.getElementById('root');
    const key = root && Object.keys(root).find(k => k.startsWith('__reactContainer'));
    if (!key) return null;
    const queue = [root[key]];
    for (let i = 0; i < queue.length && i < 60000; i++) {
      const f = queue[i];
      if (!f) continue;
      try {
        const n = f.stateNode;
        if (n && typeof n === 'object' && !(n instanceof Node)
            && typeof n.connectToHostsImpl === 'function') return n;
      } catch (e) {}
      if (f.child) queue.push(f.child);
      if (f.sibling) queue.push(f.sibling);
    }
    return null;
  };

  window.__kc = {
    version: 1,

    startCapture(id, token) {
      if (!token || captures.has(token)) throw new Error('unique capture token required');
      for (const [key, c] of captures) {
        if (Date.now() - c.created > 900000) { releaseCapture(c); captures.delete(key); }
      }
      if (captures.size >= 128) throw new Error('too many active captures');
      const terminal = api(id)?.terminal;
      if (!terminal || terminal.buffer.active.type !== 'normal') throw new Error('normal buffer required for capture');
      if (typeof terminal.registerMarker !== 'function') throw new Error('marker tracking unavailable');
      const marker = terminal.registerMarker(0);
      if (!marker || marker.isDisposed) throw new Error('marker tracking unavailable');
      const c = {id, terminal, marker, created: Date.now(), resized: false};
      if (typeof terminal.onResize === 'function') c.resize = terminal.onResize(() => { c.resized = true; });
      captures.set(token, c);
      return {token};
    },

    resetCapture(id, token) {
      const c = capture(id, token);
      const marker = c.terminal.registerMarker(0);
      if (!marker || marker.isDisposed) throw new Error('marker tracking unavailable');
      c.marker.dispose();
      c.marker = marker;
      c.resized = false;
      return true;
    },

    readCapture(id, token) {
      const c = capture(id, token);
      const lost = c.marker.isDisposed || c.resized;
      return {...this.read(id, lost ? 0 : c.marker.line + 1), history_lost: lost};
    },

    stopCapture(id, token) {
      const c = captures.get(token);
      if (!c) return false;
      if (c.id !== id) throw new Error('capture belongs to another session');
      releaseCapture(c);
      captures.delete(token);
      return true;
    },

    hosts() {
      return state().entities.hosts.map(h => {
        const cfg = h.ssh_config || {};
        const ident = identityOf(cfg);
        return {
          label: h.label || null,
          address: h.address || null,
          port: cfg.port || null,
          group: h.group ? h.group.label : null,
          username: ident ? ident.username || null : null,
          identity: ident ? ident.label || null : null,
          auth: ident ? (ident.ssh_key ? 'key' : ident.password ? 'password' : 'none') : 'none',
          jump_hosts: (h.connectionPath || []).length > 1 ? (h.connectionPath.length - 1) : 0,
          os: h.os_name || null,
        };
      });
    },

    keychain() {
      const e = state().entities;
      const label = o => (o && o.label) || null;
      return {
        identities: (e.visibleIdentities || []).map(i => ({
          label: i.label || null,
          username: i.username || null,
          has_password: !!i.password,
          ssh_key: label(i.ssh_key),
        })),
        ssh_keys: (e.sshKeys || []).map(k => ({ label: label(k) })),
        hardware_keys: (e.hardwareKeys || []).map(k => ({ label: label(k) })),
        known_hosts: (e.knownHosts || []).map(k => ({
          hostnames: k.hostnames || null,
          key_type: k.key ? String(k.key).split(' ')[0] : null,
        })),
      };
    },

    sessions() {
      const t = state().terminal;
      return Object.values(t.all || {}).map(s => ({
        id: s.id,
        title: s.title || null,
        // Local tabs have no saved host; their tab title is the only name.
        name: (s.host && s.host.label) || s.title || s.id.slice(0, 8),
        protocol: s.connectionProtocol || null,
        host: s.host ? s.host.label || null : null,
        address: s.host ? s.host.address || null : null,
        connected: !!s.connectionEstablished,
        active: !!s.isActive,
        // reachable = we hold its xterm (pool or DOM); mounted = its tab is in the DOM now.
        rendered: !!api(s.id),
        via: apiFromPool(s.id) ? 'pool' : (api(s.id) ? 'dom' : null),
        mounted: (() => { const a = api(s.id); return !!(a && a.terminal.element
          && document.contains(a.terminal.element)); })(),
      }));
    },

    // Logical lines (soft-wrapped rows joined) from absolute row `from`
    // (default: top of the visible screen) to the end. `prompt_row` / `prompt_line`
    // are the logical line holding the cursor.
    read(id, from) {
      const a = api(id);
      if (!a) throw new Error('terminal not found in Termius (neither mediator pool nor DOM) — switch to its tab once');
      const b = a.terminal.buffer.active;
      let start = from == null ? b.baseY : Math.max(0, from);
      while (start > 0 && b.getLine(start) && b.getLine(start).isWrapped) start--;
      let pr = b.baseY + b.cursorY;
      while (pr > 0 && b.getLine(pr) && b.getLine(pr).isWrapped) pr--;
      // Text on the cursor row up to the cursor. A real shell prompt leaves the cursor
      // just after "<symbol> "; a program's progress line ("45%") does not.
      const curRow = b.getLine(b.baseY + b.cursorY);
      return {
        lines: logical(b, start, b.length),
        prompt_row: pr,
        prompt_line: logical(b, pr, b.length)[0] || '',
        cursor_row: b.baseY + b.cursorY,
        cursor_x: b.cursorX,
        before_cursor: curRow ? curRow.translateToString(false, 0, b.cursorX) : '',
        cols: a.terminal.cols,
      };
    },

    scrollback(id) {
      const a = api(id);
      if (!a) throw new Error('terminal not found in Termius (neither mediator pool nor DOM) — switch to its tab once');
      const b = a.terminal.buffer.active;
      return { lines: logical(b, 0, b.length) };
    },

    // Open a SAVED host (exact label, case-insensitive, must be unique) in a new tab of
    // this window. Never a free-form address and never a new window: a second main
    // window would be invisible to this bridge. dryRun resolves everything and connects
    // to nothing.
    connectHost(label, dryRun) {
      const want = String(label || '').trim().toLowerCase();
      if (!want) throw new Error('host label is empty');
      const hits = state().entities.hosts.filter(h => String(h.label || '').toLowerCase() === want);
      if (hits.length === 0) throw new Error(`no saved host is labelled ${JSON.stringify(label)} (see list_hosts)`);
      if (hits.length > 1) throw new Error(`${hits.length} saved hosts are labelled ${JSON.stringify(label)}; rename one in Termius`);
      const list = findHostsList();
      if (!list) throw new Error('Termius hosts list not found — open the Hosts screen once, then retry');
      const h = hits[0];
      // The redux copy is a plain object. The app's connect code calls getters that only
      // exist on its own Host instances (getHostProtocol reads host.actions[0]), so the
      // live instance that the Hosts list itself renders is what must be passed in.
      const live = ((list.props && list.props.hosts) || []).filter(x => x.local_id === h.local_id);
      if (live.length !== 1) throw new Error(`Hosts list holds ${live.length} live entries for ${JSON.stringify(h.label)}; nothing was opened`);
      if (!dryRun) {
        // connectionOrigin is analytics metadata; reuse a value the app itself sends.
        list.connectToHostsImpl({ hosts: [live[0]], newWindow: false, connectionOrigin: 'Edit Host Screen' });
      }
      return { label: h.label || null, address: h.address || null, dry_run: !!dryRun };
    },

    // First-time host-key prompt ("The authenticity of <address> can not be established").
    // Answers ONLY that prompt, and only when exactly one such prompt names `address`.
    // A changed-key warning is a different dialog with different text: never matched.
    // mode 'add' presses "Add and continue" (remembers the key, so a later change is
    // caught); 'once' presses "Continue". Returns null when no prompt names the address.
    answerHostKey(address, mode) {
      const label = { add: 'Add and continue', once: 'Continue' }[mode];
      if (!label) throw new Error(`unknown host-key mode ${JSON.stringify(mode)}`);
      const want = String(address || '').trim();
      if (!want) throw new Error('address is empty');
      const prompts = [];
      for (const b of document.querySelectorAll('button')) {
        if ((b.textContent || '').trim() !== 'Add and continue') continue;
        let n = b;
        for (let i = 0; i < 12 && n.parentElement; i++) {
          n = n.parentElement;
          if (/can not be established/i.test(n.textContent || '')) break;
        }
        const t = n.textContent || '';
        const m = /authenticity of (\S+) can not be established/i.exec(t);
        if (!m || m[1] !== want) continue;
        const btn = [...n.querySelectorAll('button')].filter(x => (x.textContent || '').trim() === label);
        if (btn.length !== 1) throw new Error(`host-key prompt has ${btn.length} "${label}" buttons; nothing was pressed`);
        const fp = /SHA256:\s*([A-Za-z0-9+\/=]+?)\s*Do you want/i.exec(t);
        prompts.push({ btn: btn[0], fingerprint: fp ? 'SHA256:' + fp[1] : null });
      }
      if (prompts.length === 0) return null;
      if (prompts.length > 1) throw new Error(`${prompts.length} host-key prompts name ${want}; nothing was pressed (answer them in Termius)`);
      prompts[0].btn.click();
      return { address: want, fingerprint: prompts[0].fingerprint, pressed: label };
    },

    input(id, data) {
      const a = api(id);
      if (!a) throw new Error('terminal not found in Termius (neither mediator pool nor DOM) — switch to its tab once');
      a.terminal.input(data);
      return true;
    },
  };
  return true;
})()
