"""Shared fixtures: an isolated lock directory, and a fake Termius that stands in for
the CDP bridge so the tools can be exercised without Termius running."""
import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class FakeTerm:
    """One terminal tab. `input()` mimics a shell: typed text lands on the prompt
    line, "\\r" runs it. `busy=True` models a foreground command that has not
    returned (the cursor line is empty, no prompt)."""

    def __init__(self, tab_id, name="Local Terminal", protocol="local", prompt="me@box %"):
        self.id, self.name, self.protocol, self.prompt = tab_id, name, protocol, prompt
        self.lines = [prompt + " "]
        self.busy = False
        self.on_read = None
        self.sent = []                     # every chunk written via input()
        self.outputs = {}                  # command -> output lines

    @property
    def cursor_row(self):
        return len(self.lines) - 1

    def input(self, data):
        self.sent.append(data)
        if data == "\r":
            cmd = self.lines[-1][len(self.prompt) + 1:]
            self.lines += self.outputs.get(cmd, [])
            self.lines.append(self.prompt + " ")
        elif data == "\x15":
            self.lines[-1] = self.prompt + " "
        else:
            self.lines[-1] += data


class FakeBridge:
    """Quacks like server.Page for the tools (call(fn, *args))."""

    def __init__(self, terms, windows=1):
        self.terms = {t.id: t for t in terms}
        self.windows = windows
        self.active = terms[0].id

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def call(self, fn, *args):
        return getattr(self, "_" + fn)(*args)

    def _sessions(self):
        return [{"id": t.id, "title": t.name, "name": t.name, "protocol": t.protocol,
                 "host": None, "address": None, "connected": True,
                 "active": t.id == self.active, "rendered": True, "via": "pool",
                 "mounted": True} for t in self.terms.values()]

    def _read(self, tid, start=None):
        t = self.terms[tid]
        if t.on_read:
            t.on_read(t)                   # simulate a program still drawing
        top = 0 if start is None else start
        last = t.lines[-1]
        # a busy terminal has an empty cursor line
        lines = t.lines[top:] if not t.busy else t.lines[top:] + [""]
        return {"lines": lines, "prompt_row": t.cursor_row,
                "prompt_line": "" if t.busy else last,
                "before_cursor": "" if t.busy else last,   # cursor sits at the end of the line
                "cursor_x": 0 if t.busy else len(last),
                "cursor_row": t.cursor_row + (1 if t.busy else 0), "cols": 120}

    def _scrollback(self, tid):
        return {"lines": list(self.terms[tid].lines)}

    def _input(self, tid, data):
        self.terms[tid].input(data)
        return True

    def _startCapture(self, tid, token):
        if not hasattr(self, "captures"):
            self.captures = {}
        self.captures[token] = (tid, self.terms[tid].cursor_row)
        return {"token": token}

    def _resetCapture(self, tid, token):
        self.captures[token] = (tid, self.terms[tid].cursor_row)
        return True

    def _readCapture(self, tid, token):
        owner, start = self.captures[token]
        assert owner == tid
        return {**self._read(tid, start + 1), "history_lost": False}

    def _stopCapture(self, tid, token):
        self.captures.pop(token, None)
        return True


@pytest.fixture
def lockdir(tmp_path, monkeypatch):
    d = tmp_path / "locks"
    d.mkdir(mode=0o700)
    monkeypatch.setattr(server, "LOCK_DIR", d)
    monkeypatch.setattr(server, "LOCK_WAIT", 0.3)
    return d


@pytest.fixture
def term():
    return FakeTerm("aaaa1111-0000-0000-0000-000000000001")


@pytest.fixture
def bridge(monkeypatch, lockdir, term):
    other = FakeTerm("bbbb2222-0000-0000-0000-000000000002", name="web-1", protocol="ssh",
                     prompt="deploy@web-1 $")
    b = FakeBridge([term, other])
    monkeypatch.setattr(server, "Page", lambda: b)
    return b


def foreign_claim(tab_id, pid=None, last=None, token="someone-else"):
    """Write a claim as if another session's MCP process held it."""
    server.LOCK_DIR.mkdir(mode=0o700, exist_ok=True)
    claim = {"pid": pid if pid is not None else os.getppid(), "token": token,
             "session": "runtime-other", "since": time.time(),
             "last": last if last is not None else time.time()}
    import json
    server._claim_path(tab_id).write_text(json.dumps(claim))
    return claim
