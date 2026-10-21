#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp==1.27.0", "websockets==17.2"]
# ///
"""Minimal MCP server that drives an already-open Termius session over CDP.

Termius must run with its debug port open:
    open -a /Applications/Termius.app --args --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222

Terminal output is rendered buffer text: ANSI and trailing whitespace are
removed and long output is capped. run_command preserves interior blank lines.
Host and keychain listings return whitelisted metadata, not credentials.
Sensitive text printed inside a terminal can still appear in terminal output.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import plistlib
import posixpath
import re
import time
import urllib.request
from urllib.parse import urlparse
import uuid
from pathlib import Path

import websockets
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

PORT = int(os.environ.get("TERMIUS_CDP_PORT", "9222"))
TERMIUS_ORIGIN = "file:///Applications/Termius.app/"
BRIDGE = ((Path(__file__).parent / "bridge.js").read_text()
          + ";\n" + (Path(__file__).parent / "native.js").read_text())
BRIDGE_ID = hashlib.sha1(BRIDGE.encode()).hexdigest()[:12]

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[=>()][0-9A-Za-z]?")
PROMPT_TAIL = re.compile(r"[$#%>❯]\s*$")
# oh-my-zsh robbyrussell ("➜  ~", "➜  repo git:(main) ✗"). Its prompt ends in a
# directory name, so only a FULL-line match counts: anything typed after it breaks it.
PROMPT_OMZ = re.compile(r"^➜ +\S+( git:\([^)]*\))?( ✗)?$")


def _is_prompt(line: str) -> bool:
    return bool(line) and bool(PROMPT_TAIL.search(line) or PROMPT_OMZ.match(line))


def _at_prompt(read: dict) -> bool:
    """Cursor is parked right after a prompt: the cursor row's text up to the cursor ends
    in a space, is the end of the (trimmed) prompt line, and that line looks like a prompt.
    Output such as "Downloading... 45%" fails: no space after the symbol."""
    line = ANSI.sub("", read["prompt_line"]).rstrip()
    before = ANSI.sub("", read.get("before_cursor", ""))
    return (_is_prompt(line) and before.endswith(" ")
            and bool(before.strip()) and line.endswith(before.rstrip()))
MAX_LINES = 200

KEYS = {
    "enter": "\r", "tab": "\t", "esc": "\x1b", "backspace": "\x7f",
    "ctrl-c": "\x03", "ctrl-d": "\x04", "ctrl-u": "\x15", "ctrl-l": "\x0c",
    "ctrl-z": "\x1a", "ctrl-r": "\x12",
    "up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C", "left": "\x1b[D",
    "q": "q", "y": "y", "n": "n",
}

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
TYPE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)
EXEC = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)
CLAIM = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                        openWorldHint=False)

mcp = FastMCP("termius")


# ---------- CDP plumbing ----------------------------------------------------

def _target_ws() -> tuple[str, int]:
    """(websocket url, number of Termius main windows on the port).

    The tools only ever talk to ONE renderer. With several main windows the others'
    tabs are invisible, so the caller must know there is more than one and refuse to
    type. The window is picked by sorted URL so repeated calls hit the same one."""
    try:
        targets = json.load(urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}/json/list", timeout=3))
    except OSError:
        raise RuntimeError(
            f"Termius debug port {PORT} is not open. Enabling --remote-debugging-port "
            "requires restarting Termius and exposes renderer access to local processes. "
            "Ask the user before restarting or exposing the port.")
    # Refuse anything that is not Termius' own primary window.
    primary = sorted((t for t in targets
                      if t.get("url", "").startswith(TERMIUS_ORIGIN)
                      and "type=primary" in t.get("url", "")),
                     key=lambda t: t["url"])
    if not primary:
        raise RuntimeError(f"Port {PORT} is open but no Termius main window is on it.")
    url = primary[0]["webSocketDebuggerUrl"]
    parsed = urlparse(url)
    if (parsed.scheme != "ws" or parsed.hostname != "127.0.0.1" or parsed.port != PORT
            or parsed.username or parsed.password):
        raise RuntimeError("CDP must use the configured loopback endpoint")
    return url, len(primary)


class Page:
    def __init__(self) -> None:
        self.ws = None
        self.n = 0
        self.windows = 1

    async def __aenter__(self):
        info = Path('/Applications/Termius.app/Contents/Info.plist')
        with info.open('rb') as f:
            version = plistlib.load(f).get('CFBundleShortVersionString')
        if version != '9.38.1':
            raise RuntimeError(f'This adapter targets Termius 9.38.1; found {version}. Review compatibility before enabling it.')
        url, self.windows = _target_ws()
        self.ws = await websockets.connect(url, max_size=64 * 1024 * 1024)
        try:
            if await self.eval("window.__kcId || null") != BRIDGE_ID:
                await self.eval(BRIDGE)
                await self.eval(f"window.__kcId = {json.dumps(BRIDGE_ID)}")
        except BaseException:
            await self.ws.close()
            raise
        return self

    async def __aexit__(self, *exc):
        await self.ws.close()

    async def eval(self, expr: str):
        async with asyncio.timeout(15):
            return await self._eval(expr)

    async def _eval(self, expr: str):
        self.n += 1
        await self.ws.send(json.dumps({
            "id": self.n, "method": "Runtime.evaluate",
            "params": {"expression": expr, "returnByValue": True, "awaitPromise": True},
        }))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") != self.n:
                continue
            if "error" in msg:
                raise RuntimeError("CDP evaluation failed: " + str(msg["error"].get("message", "unknown error")))
            res = msg.get("result", {})
            if "exceptionDetails" in res:
                d = res["exceptionDetails"]
                text = (d.get("exception") or {}).get("description") or d.get("text")
                raise RuntimeError(str(text).splitlines()[0])
            return res.get("result", {}).get("value")

    async def call(self, fn: str, *args):
        return await self.eval(f"window.__kc.{fn}({', '.join(json.dumps(a) for a in args)})")


# ---------- filtering -------------------------------------------------------

def _clean(lines: list[str]) -> list[str]:
    out, blank = [], False
    for ln in lines:
        ln = ANSI.sub("", ln).rstrip()
        if not ln:
            if blank:
                continue
            blank = True
        else:
            blank = False
        out.append(ln)
    while out and not out[-1]:
        out.pop()
    while out and not out[0]:
        out.pop(0)
    return out


def _cap(lines: list[str], limit: int) -> str:
    if len(lines) <= limit:
        return "\n".join(lines)
    head = limit // 4
    tail = limit - head
    omitted = len(lines) - limit
    return "\n".join(lines[:head] + [f"... [{omitted} lines omitted — aggregate on the "
                                     f"remote side (grep/awk/tail) to see less] ..."] + lines[-tail:])


def _window_warning(page: Page) -> str:
    if page.windows <= 1:
        return ""
    return (f"WARNING: {page.windows} Termius main windows are open; only one is visible "
            "to this server, so tabs in the others are not listed. Close the extra "
            "windows to type.")


async def _resolve(page: Page, session: str | None, strict: bool = False) -> dict:
    # strict = the caller is about to type (or claim). Never type while part of the
    # tab list is invisible: a name that matches one tab here may exist twice.
    if strict and page.windows > 1:
        raise RuntimeError(_window_warning(page) + " Nothing was sent.")
    sessions = await page.call("sessions")
    if not sessions:
        raise RuntimeError("No open Termius session. Open one in Termius first.")
    if session:
        key = session.lower()
        exact = [s for s in sessions
                 if key in (s["id"].lower(), (s["name"] or "").lower(),
                            (s["title"] or "").lower(), (s["address"] or "").lower())]
        hits = exact or [s for s in sessions if s["id"].lower().startswith(key)]
        if len(hits) == 1:
            return hits[0]
        what = "matches several" if hits else "matches no"
        raise RuntimeError(f"{session!r} {what} session. Open: "
                           + ", ".join(f"{s['name']} ({s['id'][:8]})" for s in sessions))
    if len(sessions) == 1:
        return sessions[0]
    # Input tools must name the target: falling back to the active tab would send
    # keystrokes to whichever host the human last clicked, with no warning.
    active = [s for s in sessions if s["active"]]
    if not strict and len(active) == 1:
        return active[0]
    raise RuntimeError("Several sessions are open; pass `session`: "
                       + ", ".join(f"{s['name']} ({s['id'][:8]})" for s in sessions))


# Fixed path, NOT tempfile.gettempdir(): KiroCrew gives every session its own
# TMPDIR, so a TMPDIR-based lock dir would never be shared between sessions.
LOCK_DIR = Path(os.environ.get("TERMIUS_LOCK_DIR", f"/tmp/termius-mcp-locks-{os.getuid()}"))
LOCK_WAIT = float(os.environ.get("TERMIUS_LOCK_WAIT", "5"))
# run_command requires the screen to stay unchanged this long before it types.
SETTLE = float(os.environ.get("TERMIUS_SETTLE", "0.3"))


@contextlib.asynccontextmanager
async def _tab_lock(s: dict):
    """One input call per tab at a time. Every KiroCrew session spawns its own MCP
    process, so an asyncio.Lock would only cover one of them: use flock on a file per
    tab id, which the kernel releases even if the process dies mid-call. Wait briefly
    (a short send_key finishing), then refuse rather than queue behind a long command.
    Humans typing in Termius are not covered -- this only serialises MCP callers."""
    LOCK_DIR.mkdir(mode=0o700, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", s["id"])
    fd = os.open(LOCK_DIR / f"{safe}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if loop.time() >= deadline:
                    raise RuntimeError(f"session {s['name']} is busy with another input call "
                                       "(run_command / send_key / send_text); nothing was sent. "
                                       "Check with read_terminal and retry.") from None
                await asyncio.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------- tab claims ------------------------------------------------------
# A call-level lock cannot stop session B from slipping a command between two of
# session A's calls (A: `cd /srv/app`, B: `cd /tmp`, A: `rm -rf cache/`). So an
# input tool only works on a tab THIS process has claimed. One MCP process per
# KiroCrew session, so the process is the owner; a claim dies with its process or
# after CLAIM_IDLE seconds without an input call from the owner.

# Longer than run_command's 600 s cap, so a claim cannot expire while its owner's
# command is still running (the timer is refreshed when the call starts).
CLAIM_IDLE = int(os.environ.get("TERMIUS_CLAIM_IDLE", "900"))
OWNER = {"pid": os.getpid(), "token": uuid.uuid4().hex,
         # The per-session scratch dir is the only human-recognisable session handle here.
         "session": Path(os.environ.get("TMPDIR", "?").rstrip("/")).name}


@contextlib.contextmanager
def _claims_mutex():
    LOCK_DIR.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(LOCK_DIR / "claims.mutex", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)      # held for a file read/write only
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _claim_path(tab_id: str) -> Path:
    return LOCK_DIR / (re.sub(r"[^A-Za-z0-9_-]", "_", tab_id) + ".claim")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _read_claim(tab_id: str) -> dict | None:
    """The live claim on a tab, or None if unclaimed / owner gone / idle too long."""
    try:
        c = json.loads(_claim_path(tab_id).read_text())
    except (FileNotFoundError, ValueError):
        return None
    if not _alive(int(c.get("pid", 0))) or time.time() - c.get("last", 0) > CLAIM_IDLE:
        return None
    return c


def _write_claim(tab_id: str, since: float | None = None) -> None:
    now = time.time()
    p = _claim_path(tab_id)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({**OWNER, "since": since or now, "last": now}))
    os.replace(tmp, p)


def _opened_path(tab_id: str) -> Path:
    return LOCK_DIR / (re.sub(r"[^A-Za-z0-9_-]", "_", tab_id) + ".opened")


def _mark_opened(tab_id: str) -> None:
    """Record that connect_host (not a human) opened this tab. Survives the opener's exit, so
    a later session can reuse the tab; whether it may is still decided by the claim."""
    LOCK_DIR.mkdir(mode=0o700, exist_ok=True)
    p = _opened_path(tab_id)
    tmp = p.with_suffix(".otmp")
    tmp.write_text(json.dumps({"session": OWNER["session"], "token": OWNER["token"],
                               "pid": OWNER["pid"], "at": time.time()}))
    os.replace(tmp, p)


def _read_opened(tab_id: str) -> dict | None:
    try:
        return json.loads(_opened_path(tab_id).read_text())
    except (FileNotFoundError, ValueError):
        return None


def _mine(c: dict | None) -> bool:
    return bool(c) and c.get("token") == OWNER["token"]


def _describe(c: dict) -> str:
    return (f"session {c.get('session')} (pid {c.get('pid')}), "
            f"idle {int(time.time() - c['last'])}s of {CLAIM_IDLE}s")


def _require_claim(s: dict) -> None:
    """Refuse input unless this process holds the tab; refresh the idle timer."""
    with _claims_mutex():
        c = _read_claim(s["id"])
        if not _mine(c):
            who = f"claimed by {_describe(c)}" if c else "not claimed"
            raise RuntimeError(f"session {s['name']} is {who}; nothing was sent. "
                               "Call claim_terminal first"
                               + (" or wait for it to be released." if c else "."))
        _write_claim(s["id"], c["since"])


def _touch_claim(s: dict) -> None:
    with _claims_mutex():
        c = _read_claim(s["id"])
        if _mine(c):
            _write_claim(s["id"], c["since"])


# ---------- tools -----------------------------------------------------------

@mcp.tool(annotations=READ)
async def list_hosts(query: str | None = None) -> str:
    """List saved Termius hosts: label, address, port, group, username, auth type.
    Passwords and keys are never included. `query` filters by label/address/group."""
    async with Page() as p:
        hosts = await p.call("hosts")
    if query:
        q = query.lower()
        hosts = [h for h in hosts if any(q in str(h.get(k) or "").lower()
                                         for k in ("label", "address", "group"))]
    rows = [f"{h['label']}\t{h['username'] or '-'}@{h['address']}:{h['port'] or 22}"
            f"\tgroup={h['group'] or '-'}\tauth={h['auth']}"
            + (f"\tjump={h['jump_hosts']}" if h["jump_hosts"] else "")
            for h in hosts]
    return "\n".join(rows) or "(no hosts)"


CONNECT_WAIT_MAX = 120


def _connect_allowed(label: str) -> bool:
    """TERMIUS_CONNECT_ALLOW (comma separated labels) limits connect_host. Unset = any
    saved host; the client's per-call approval is then the only gate."""
    raw = os.environ.get("TERMIUS_CONNECT_ALLOW", "").strip()
    if not raw:
        return True
    return label.strip().lower() in {x.strip().lower() for x in raw.split(",") if x.strip()}


def _hostkey_mode() -> str:
    """TERMIUS_HOSTKEY: what connect_host does with a first-time host-key prompt.
    ask (default) = press nothing. add = press "Add and continue" (trust on first use, key remembered so a
    later change is NOT auto-accepted); once = press "Continue"; ask = press nothing."""
    m = os.environ.get("TERMIUS_HOSTKEY", "ask").strip().lower()
    return m if m in ("add", "once", "ask") else "ask"


@contextlib.asynccontextmanager
async def _connect_lock():
    """One connect at a time across all MCP processes: the new tab is found by diffing
    the tab list, so two simultaneous connects could each take the other's tab."""
    LOCK_DIR.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(LOCK_DIR / "connect.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 30
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if loop.time() >= deadline:
                    raise RuntimeError("another connect_host call is in progress; "
                                       "nothing was opened. Retry shortly.") from None
                await asyncio.sleep(0.2)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _reuse_tab(sessions: list[dict], want: str, label: str, claim: bool) -> dict | None:
    """A connected tab of host `want` this session opened and may use, claimed if `claim`.
    Own claim first, then an unclaimed one. None = no tab of that host at all (open one).
    Raises when tabs exist but cannot be used safely, so the caller never opens a duplicate
    behind another session's back."""
    # Only tabs THIS session opened through connect_host. A tab a human opened, or one another
    # session opened, is never taken implicitly (claim_terminal does that, explicitly).
    tabs = [s for s in sessions if (s["host"] or "").lower() == want and s["connected"]
            and (_read_opened(s["id"]) or {}).get("token") == OWNER["token"]]
    if not tabs:
        return None
    with _claims_mutex():
        claims = {t["id"]: _read_claim(t["id"]) for t in tabs}
        mine = [t for t in tabs if _mine(claims[t["id"]])]
        free = [t for t in tabs if not claims[t["id"]]]
        pick = mine or free
        if len(pick) == 1:
            if claim:
                t = pick[0]
                _write_claim(t["id"], claims[t["id"]]["since"] if claims[t["id"]] else None)
            return pick[0]
        if len(pick) > 1:
            ids = ", ".join(t["id"][:8] for t in pick)
            raise RuntimeError(f"{len(pick)} tabs of {label} are available ({ids}); nothing was "
                               "opened or claimed. Pick one with claim_terminal, or pass "
                               "new_tab=true.")
        held = "; ".join(f"{t['id'][:8]} by {_describe(claims[t['id']])}" for t in tabs)
        raise RuntimeError(f"every connected tab of {label} is claimed by another session "
                           f"({held}); nothing was opened. Wait for release, or pass "
                           "new_tab=true to open a separate tab.")


@mcp.tool(annotations=EXEC)
async def connect_host(host: str, dry_run: bool = False, wait: int = 30,
                       new_tab: bool = False) -> str:
    """Get a tab for a SAVED Termius host (exact label from list_hosts). A tab that
    THIS session's connect_host opened earlier (list_terminal_sessions marks it "opened by
    agent: THIS session") and that nobody else has claimed is REUSED; otherwise a NEW TAB is
    opened (wait until connected, then claim). Tabs a human or another session opened are
    never reused implicitly. `new_tab=true` always opens a new tab. A tab claimed by another session is
    never taken: the call fails and says who holds it.
    Termius' own saved identity / key / jump hosts are used; no credential is read or
    passed here. Only saved hosts: no free-form address, no new window. A first-time
    host-key prompt is answered per TERMIUS_HOSTKEY (default: "ask", the
    fingerprint is reported back; a CHANGED key is never accepted). A password prompt
    is NOT answered: read_terminal and ask the human.
    `dry_run=true` only checks that the host and Termius' connect function are found.
    TERMIUS_CONNECT_ALLOW (comma separated labels) can restrict which hosts are allowed."""
    if not _connect_allowed(host):
        raise RuntimeError(f"host {host!r} is not in TERMIUS_CONNECT_ALLOW; nothing was opened.")
    wait = max(1, min(int(wait), CONNECT_WAIT_MAX))
    async with Page() as p:
        # Several main windows: the new tab might land in one this server cannot see.
        if p.windows > 1:
            raise RuntimeError(_window_warning(p) + " Nothing was opened.")
        async with _connect_lock():
            existing = await p.call("sessions")
            before = {s["id"] for s in existing}
            # Resolve the label first (dry run opens nothing) so reuse can be checked.
            res = await p.call("connectHost", host, True)
            want = (res["label"] or "").lower()
            if not new_tab:
                reuse = _reuse_tab(existing, want, res["label"], claim=not dry_run)
                if reuse:
                    if dry_run:
                        return (f"dry run ok: would reuse tab {reuse['id'][:8]} "
                                f"({res['label']}); nothing was opened or claimed.")
                    return (f"[{reuse['name']}] reused existing tab {reuse['id'][:8]} - connected. "
                            f"Claimed by this session; use session='{reuse['id'][:8]}'. "
                            "Pass new_tab=true to open a separate tab instead.")
            if dry_run:
                return (f"dry run ok: would open {res['label']} ({res['address']}) "
                        "in a new tab; nothing was opened.")
            res = await p.call("connectHost", host, False)
            want = (res["label"] or "").lower()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + wait
            new: list[dict] = []
            hostkey = None          # what we pressed on a first-time host-key prompt
            hostkey_err = None
            mode = _hostkey_mode()
            while True:
                await asyncio.sleep(0.5)
                new = [s for s in await p.call("sessions")
                       if s["id"] not in before and (s["host"] or "").lower() == want]
                if len(new) == 1 and not new[0]["connected"] and mode != "ask" \
                        and hostkey is None and hostkey_err is None:
                    try:
                        hostkey = await p.call("answerHostKey", res["address"], mode)
                    except RuntimeError as e:      # several prompts name it: leave to the human
                        hostkey_err = str(e)
                if len(new) > 1 or (len(new) == 1 and new[0]["connected"]) \
                        or loop.time() >= deadline:
                    break
            if len(new) != 1:
                what = "several new tabs appeared" if new else "no new tab appeared"
                raise RuntimeError(f"{what} for {res['label']} within {wait}s; nothing was "
                                   "claimed. Check list_terminal_sessions.")
            s = new[0]
            with _claims_mutex():
                _mark_opened(s["id"])
                _write_claim(s["id"])
    state = ("connected" if s["connected"]
             else f"NOT connected after {wait}s (host-key / password prompt or a failure? "
                  "read_terminal, do not guess an answer)")
    note = ""
    if hostkey:
        note = (f" First-time host key {hostkey['fingerprint'] or '(fingerprint unreadable)'} "
                f"for {hostkey['address']} was accepted via \"{hostkey['pressed']}\" "
                "(TERMIUS_HOSTKEY); verify it if this host is new to you.")
    elif hostkey_err:
        note = f" Host-key prompt left unanswered: {hostkey_err}"
    return (f"[{s['name']}] opened as tab {s['id'][:8]} — {state}.{note} Claimed by this "
            f"session; use session='{s['id'][:8]}'.")


@mcp.tool(annotations=READ)
async def list_keychain() -> str:
    """List Termius keychain entries by name only: identities (with whether a password
    is stored), SSH keys, hardware keys, known hosts. No secret material is returned."""
    async with Page() as p:
        k = await p.call("keychain")
    out = ["identities:"]
    out += [f"  {i['label']}\tuser={i['username'] or '-'}\tpassword={'yes' if i['has_password'] else 'no'}"
            f"\tkey={i['ssh_key'] or '-'}" for i in k["identities"]] or ["  (none)"]
    out.append("ssh_keys:")
    out += [f"  {x['label']}" for x in k["ssh_keys"]] or ["  (none)"]
    if k["hardware_keys"]:
        out.append("hardware_keys:")
        out += [f"  {x['label']}" for x in k["hardware_keys"]]
    out.append("known_hosts:")
    out += [f"  {x['hostnames']}\t{x['key_type'] or '-'}" for x in k["known_hosts"]] or ["  (none)"]
    return "\n".join(out)


@mcp.tool(annotations=READ)
async def list_terminal_sessions() -> str:
    """List terminal sessions open in Termius (SSH and local tabs): id prefix, name,
    protocol, address, connection state. Any of id prefix / name / address can be
    passed as `session` to the other tools."""
    async with Page() as p:
        ss = await p.call("sessions")
        warn = _window_warning(p)
    if not ss:
        return "(no open sessions — open one in Termius)"
    rows = []
    for s in ss:
        cols = [s["id"][:8], s["name"], s["protocol"] or "-", s["address"] or "-",
                "connected" if s["connected"] else "DISCONNECTED"]
        if s["active"]:
            cols.append("active")
        if not s["rendered"]:
            cols.append("(terminal unreachable)")
        elif not s.get("mounted", True):
            cols.append("background")
        with _claims_mutex():
            c = _read_claim(s["id"])
        o = _read_opened(s["id"])
        if o:
            cols.append("opened by agent: THIS session" if o.get("token") == OWNER["token"]
                        else f"opened by agent: session {o.get('session')}")
        if c:
            cols.append("claimed by THIS session" if _mine(c) else f"claimed by {_describe(c)}")
        rows.append("\t".join(cols))
    return "\n".join(rows + ([warn] if warn else []))


@mcp.tool(annotations=CLAIM)
async def claim_terminal(session: str) -> str:
    """Claim a Termius tab for this session. run_command / send_key / send_text refuse
    any tab this session has not claimed, so another agent cannot slip commands in
    between yours. Refused while another live session holds it. A claim ends with
    release_terminal, when this session's MCP process exits, or after 15 min with no
    input call. Prefer a tab of your own over one a human or another agent is using."""
    async with Page() as p:
        s = await _resolve(p, session, strict=True)
    with _claims_mutex():
        c = _read_claim(s["id"])
        if c and not _mine(c):
            raise RuntimeError(f"session {s['name']} is already claimed by {_describe(c)}")
        _write_claim(s["id"], c["since"] if c else None)
    return f"[{s['name']}] claimed by this session ({OWNER['session']}, pid {OWNER['pid']})"


@mcp.tool(annotations=CLAIM)
async def release_terminal(session: str) -> str:
    """Release a tab this session claimed. Release when your work on the tab is done."""
    async with Page() as p:
        s = await _resolve(p, session, strict=True)
    with _claims_mutex():
        c = _read_claim(s["id"])
        if c and not _mine(c):
            raise RuntimeError(f"session {s['name']} is claimed by {_describe(c)}, not by "
                               "this session; only its owner can release it")
        _claim_path(s["id"]).unlink(missing_ok=True)
    return f"[{s['name']}] released" if c else f"[{s['name']}] was not claimed"


@mcp.tool(annotations=READ)
async def read_terminal(session: str | None = None, lines: int = 40, scrollback: bool = False) -> str:
    """Read terminal text (ANSI stripped, blank runs collapsed). Default: last `lines`
    lines of the screen. `scrollback=true` reads the whole history (capped)."""
    async with Page() as p:
        s = await _resolve(p, session)
        data = await p.call("scrollback" if scrollback else "read", s["id"])
        warn = _window_warning(p)
    text = _clean(data["lines"])
    limit = MAX_LINES if scrollback else max(1, min(lines, MAX_LINES))
    if not scrollback:
        text = text[-limit:]
    return f"[{s['name']}]\n" + _cap(text, limit) + (f"\n{warn}" if warn else "")


@mcp.tool(annotations=TYPE)
async def send_text(text: str, session: str | None = None) -> str:
    """Send printable text without Enter. A foreground program may act on any input.
    Use send_key('enter') to submit, or run_command to execute and collect output."""
    if "\r" in text or "\n" in text:
        raise ValueError("send_text must not contain newlines; use run_command to execute.")
    _validate_text(text)
    async with Page() as p:
        s = await _resolve(p, session, strict=True)
        async with _tab_lock(s):
            _require_claim(s)
            await p.call("input", s["id"], text)
            await asyncio.sleep(0.3)
            data = await p.call("read", s["id"])
    return f"[{s['name']}]\n" + "\n".join(_clean(data["lines"])[-3:])


@mcp.tool(annotations=TYPE)
async def send_key(key: str, session: str | None = None) -> str:
    """Send one control key: enter, tab, esc, backspace, ctrl-c, ctrl-d, ctrl-u, ctrl-l,
    ctrl-z, ctrl-r, up, down, left, right, q, y, n. Note 'enter' submits whatever is typed."""
    seq = KEYS.get(key.lower())
    if seq is None:
        raise ValueError(f"unknown key {key!r}; one of: {', '.join(KEYS)}")
    async with Page() as p:
        s = await _resolve(p, session, strict=True)
        async with _tab_lock(s):
            _require_claim(s)
            await p.call("input", s["id"], seq)
            await asyncio.sleep(0.4)
            data = await p.call("read", s["id"])
    return f"[{s['name']}]\n" + "\n".join(_clean(data["lines"])[-5:])


def _validate_text(text: str) -> None:
    if not text or any(ord(c) < 32 or 127 <= ord(c) <= 159 or c in "\u2028\u2029" for c in text):
        raise ValueError("nonempty printable text required; control characters are not allowed")


async def _run_locked(p: Page, s: dict, command: str, timeout: int):
    if not s["connected"]:
        raise RuntimeError(f"session {s['name']} is disconnected")
    before = await p.call("read", s["id"])
    prompt = ANSI.sub("", before["prompt_line"]).rstrip()
    # The cursor line must BE an idle shell prompt, or typing now would queue keystrokes
    # into a running program's stdin for the shell to execute later. Two layers, since the
    # line alone cannot tell a prompt from program output that happens to end in "%":
    #   1. the cursor sits right after "<prompt symbol> " with nothing to its right;
    #   2. the screen does not change over SETTLE seconds (a live progress bar does).
    if not _at_prompt(before):
        raise RuntimeError("cursor is not at a shell prompt (a command is still running, "
                           "or text is already typed). Check with read_terminal; use "
                           "send_key('ctrl-c') to interrupt or ctrl-u to clear the line.")
    await asyncio.sleep(SETTLE)
    again = await p.call("read", s["id"])
    if again["lines"] != before["lines"] or again["cursor_row"] != before["cursor_row"] \
            or not _at_prompt(again):
        raise RuntimeError("the screen is still changing, so something is still running "
                           "in this tab; nothing was sent. Check with read_terminal.")
    token = uuid.uuid4().hex
    # Capability check is before any input; this also anchors the terminal object.
    await p.call("startCapture", s["id"], token)
    try:
        return await _run_captured(p, s, command, timeout, token, again)
    except Exception as exc:
        raise RuntimeError("terminal input may have been delivered; inspect the session and "
                           f"do not automatically retry. {type(exc).__name__}: {exc}") from exc
    finally:
        with contextlib.suppress(Exception):
            await p.call("stopCapture", s["id"], token)


async def _run_captured(p: Page, s: dict, command: str, timeout: int, token: str, again: dict):
    start = again["prompt_row"]
    # Type first, Enter later. The echo's last row is wherever the cursor sits
    # once typing settles; output starts on the row after it. Counting rows from
    # the command length is wrong: zsh's line editor wraps by itself (rows are
    # not flagged as soft-wrapped) and CJK / emoji take two cells each.
    await p.call("input", s["id"], command)
    echo_end, prev = None, None
    for _ in range(20):
        await asyncio.sleep(0.1)
        cur = await p.call("read", s["id"], start)
        if prev is not None and cur["lines"] == prev["lines"] \
                and cur["cursor_row"] == prev["cursor_row"]:
            echo_end = cur["cursor_row"]
            break
        prev = cur
    if echo_end is None:
        await p.call("input", s["id"], "\x15")      # ctrl-u: leave the line empty
        raise RuntimeError("typed command never settled on screen; line cleared, nothing ran")
    await p.call("resetCapture", s["id"], token)
    await p.call("input", s["id"], "\r")
    status, last, stable, previous, lost = "timeout", None, 0, None, False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        await asyncio.sleep(0.25)
        try:
            data = await p.call("readCapture", s["id"], token)
        except Exception as exc:
            return "observation-lost", last, lost, str(exc)
        # Keep interior whitespace and blank lines; this is rendered buffer text,
        # not a byte-exact process log. Only empty trailing screen rows are removed.
        cur = [ANSI.sub("", line).rstrip() for line in data["lines"]]
        while cur and not cur[-1]:
            cur.pop()
        tail = cur[-1] if cur else ""
        signature = (cur, data["cursor_row"], data["cursor_x"], data.get("before_cursor"))
        stable = stable + 1 if signature == previous else 0
        previous = signature
        last = cur
        lost = lost or data.get("history_lost", False)
        # A prompt-looking last line only counts when the cursor really sits after a
        # prompt: "Downloading... 45%" stays on screen unchanged for seconds while the
        # program is still running, and used to be reported as done.
        if cur and tail == ANSI.sub("", data["prompt_line"]).rstrip() and _at_prompt(data) and stable >= 4:
            status = "prompt-observed"
            break
        if stable >= 8 and tail.endswith((":", "?", "]")):
            status = "waiting-for-input"
            break

    return status, last, lost, None


@mcp.tool(annotations=EXEC)
async def run_command(command: str, session: str | None = None, timeout: int = 30) -> str:
    """EXECUTE a shell command on the remote host in the open Termius session, wait for
    the prompt to return, and return only the command's output (echo and prompt removed).
    If it is still running or waiting for input at `timeout`, returns what it has so far
    with a status line; use read_terminal / send_key('ctrl-c') to follow up.
    The tab must be claimed by this session first (claim_terminal)."""
    if "\n" in command or "\r" in command:
        raise ValueError("one command per call (no newlines)")
    _validate_text(command)
    timeout = max(1, min(timeout, 600))
    async with Page() as p:
        s = await _resolve(p, session, strict=True)
        async with _tab_lock(s):
            _require_claim(s)
            status, last, lost, error = await _run_locked(p, s, command, timeout)
            _touch_claim(s)        # a long command must not leave the claim looking idle

    body = list(last or [])
    if status == "prompt-observed" and body:
        body = body[:-1]            # drop the fresh prompt
    header = f"[{s['name']}] $ {command}"
    if status == "prompt-observed":
        header += "\n[status: prompt-observed — heuristic only; exit code unknown]"
    else:
        header += f"\n[status: {status} — the command has NOT necessarily finished]"
    if lost:
        header += "\n[history-lost: scrollback trimmed or terminal resized; retained text only]"
    if error:
        header += f"\n[command was submitted; do not automatically retry: {error}]"
    return header + "\n" + (_cap(body, MAX_LINES) if body else "(no output)")


@mcp.tool(annotations=TYPE)
async def switch_terminal(session: str) -> str:
    """Activate an explicitly selected terminal and its containing tab; verify state."""
    async with Page() as p:
        s = await _resolve(p, session, strict=True)
        await p.call("activateSession", s["id"])
        for _ in range(20):
            sessions = await p.call("sessions")
            if any(t["id"] == s["id"] and t["active"] for t in sessions):
                return f"[{s['name']}] active (session {s['id']})"
            await asyncio.sleep(0.1)
    raise RuntimeError("activation requested but not confirmed; inspect Termius")


@mcp.tool(annotations=EXEC)
async def close_terminal(session: str, force: bool = False) -> str:
    """Close one claimed terminal session, never the whole app. Default refuses a
    busy/non-prompt terminal. force=true can interrupt work; use only when the user
    explicitly requests that. Verify actual removal, not just dispatch."""
    async with Page() as p:
        s = await _resolve(p, session, strict=True)
        async with _tab_lock(s):
            _require_claim(s)
            if s["connected"] and not force:
                before = await p.call("read", s["id"])
                await asyncio.sleep(SETTLE)
                after = await p.call("read", s["id"])
                if not _at_prompt(before) or not _at_prompt(after) or before["lines"] != after["lines"]:
                    raise RuntimeError("session may be busy or not at an idle prompt; nothing closed")
            await p.call("closeSession", s["id"])
            for _ in range(30):
                if not any(t["id"] == s["id"] for t in await p.call("sessions")):
                    with _claims_mutex():
                        if _mine(_read_claim(s["id"])):
                            _claim_path(s["id"]).unlink(missing_ok=True)
                    return f"[{s['name']}] closed (session {s['id']})"
                await asyncio.sleep(0.1)
    raise RuntimeError("close requested but not confirmed; a Termius dialog may need attention")


@mcp.tool(annotations=READ)
async def list_sftp_sessions() -> dict:
    """List currently connected SFTP panes by session ID, host and directory.
    Open Termius' SFTP view first. No credentials are returned."""
    async with Page() as p:
        return {"sessions": await p.call("sftpSessions"), "warning": _window_warning(p) or None}


@mcp.tool(annotations=READ)
async def sftp_transfer_status(transfer_id: str) -> dict:
    """Read retained native upload progress/error/completion by transfer ID. 100%
    progress or a disappeared queue row alone does not mean completed."""
    async with Page() as p:
        return await p.call("transferStatus", transfer_id)


@mcp.tool(annotations=EXEC)
async def sftp_upload(local_path: str, sftp_session: str, remote_directory: str,
                      timeout: int = 30) -> dict:
    """Upload one local regular file through Termius' native SFTP transfer queue.
    Use the exact remote session ID from list_sftp_sessions, keep one pane on
    Local, and navigate the remote pane to remote_directory first. Overwrite
    dialogs stay in Termius for the human; this tool never approves them.
    Follows the native progress/error/end events. Timeout does NOT cancel;
    follow sftp_transfer_status with the returned ID, never blindly re-upload."""
    path = Path(local_path).expanduser()
    if not path.is_absolute() or not remote_directory.startswith('/'):
        raise ValueError("local path and remote directory must be absolute")
    if any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in local_path + remote_directory):
        raise ValueError("control characters in paths are not allowed")
    if path.is_symlink() or not path.is_file():
        raise ValueError("upload source must be a regular file, not a directory or symbolic link")
    path = path.resolve(strict=True)
    token = uuid.uuid4().hex
    request = {"token": token, "session": sftp_session, "local_path": str(path),
               "local_directory": str(path.parent), "filename": path.name,
               "remote_directory": posixpath.normpath(remote_directory), "size": path.stat().st_size}
    timeout = max(1, min(int(timeout), 600))
    async with Page() as p:
        if p.windows > 1:
            raise RuntimeError(_window_warning(p) + " Nothing uploaded.")
        try:
            result = await p.call("startUpload", request)
        except (TimeoutError, websockets.ConnectionClosed) as exc:
            return {"transfer_id": token, "status": "unknown", "error": type(exc).__name__,
                    "next_step": "query this transfer ID; upload may have started, do not retry"}
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                result = await p.call("transferStatus", token)
            except (RuntimeError, TimeoutError, websockets.ConnectionClosed) as exc:
                return {"transfer_id": token, "status": "unknown", "error": str(exc),
                        "next_step": "inspect the native transfer queue; do not retry"}
            if result["status"] in {"completed", "failed", "cancelled", "incomplete", "unknown"}:
                return result
            await asyncio.sleep(0.5)
    return {**result, "transfer_id": token, "wait_timed_out": True,
            "next_step": "upload not cancelled; use sftp_transfer_status, do not re-upload"}


if __name__ == "__main__":
    mcp.run()
