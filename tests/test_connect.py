"""connect_host: opens a saved host in a new tab, claims it, and fails closed."""
import pytest

import server
from conftest import FakeBridge, FakeTerm, run


class ConnectBridge(FakeBridge):
    """FakeBridge plus a Hosts list: connecting a saved host adds a tab."""

    def __init__(self, terms, hosts, windows=1, delay_reads=0, connects=True, extra_tabs=0, prompt=None):
        super().__init__(terms, windows)
        self.prompt = prompt                    # address with a pending host-key prompt, or None
        self.answered = []
        self.hosts = hosts
        self.connect_calls = []
        self.delay_reads = delay_reads          # session reads before the tab is "connected"
        self.connects = connects
        self.extra_tabs = extra_tabs            # tabs opened per connect (>1 = ambiguous)
        self._pending = {}

    def _sessions(self):
        out = super()._sessions()
        for s in out:
            tab = self._pending.get(s["id"])
            if tab:
                s["host"], s["name"] = tab["label"], tab["label"]
                s["protocol"], s["address"] = "ssh", tab["address"]
                if tab["reads"] < self.delay_reads or (self.prompt and not self.answered):
                    s["connected"] = False
                    tab["reads"] += 1
        return out

    def _answerHostKey(self, address, mode):
        if self.prompt != address:
            return None
        self.answered.append((address, mode))
        return {"address": address, "fingerprint": "SHA256:abc", "pressed": "Add and continue"}

    def _connectHost(self, label, dry_run):
        hits = [h for h in self.hosts if h["label"].lower() == label.strip().lower()]
        if not hits:
            raise RuntimeError(f"no saved host is labelled {label!r} (see list_hosts)")
        if len(hits) > 1:
            raise RuntimeError(f"{len(hits)} saved hosts are labelled {label!r}")
        h = hits[0]
        if not dry_run:
            self.connect_calls.append(h["label"])
            if self.connects:
                for i in range(self.extra_tabs or 1):
                    t = FakeTerm(f"cccc{len(self.terms):04d}-0000-0000-0000-00000000000{i}",
                                 name=h["label"], protocol="ssh", prompt="u@h $")
                    self.terms[t.id] = t
                    self._pending[t.id] = {"label": h["label"], "address": h["address"], "reads": 0}
        return {"label": h["label"], "address": h["address"], "dry_run": bool(dry_run)}


HOSTS = [{"label": "web-1", "address": "10.0.0.1"}, {"label": "db-1", "address": "10.0.0.2"}]


@pytest.fixture
def cb(monkeypatch, lockdir):
    b = ConnectBridge([FakeTerm("aaaa1111-0000-0000-0000-000000000001")], HOSTS)
    monkeypatch.setattr(server, "Page", lambda: b)
    monkeypatch.setattr(server.asyncio, "sleep", _fast_sleep)
    monkeypatch.delenv("TERMIUS_CONNECT_ALLOW", raising=False)
    return b


_real_sleep = server.asyncio.sleep


async def _fast_sleep(t):
    await _real_sleep(0)


def new_tab(b):
    return [t for t in b.terms.values() if t.name == "web-1"][0]


def test_opens_the_tab_and_claims_it_for_this_session(cb):
    out = run(server.connect_host("web-1"))
    tab = new_tab(cb)
    assert cb.connect_calls == ["web-1"]
    assert "connected" in out and tab.id[:8] in out
    assert server._mine(server._read_claim(tab.id))
    # the claim is real: the new tab accepts run_command, the pre-existing one still refuses
    tab.outputs["echo hi"] = ["hi"]
    assert "hi" in run(server.run_command("echo hi", tab.id[:8]))
    with pytest.raises(RuntimeError, match="not claimed"):
        run(server.run_command("echo hi", "Local Terminal"))


def test_label_match_ignores_case(cb):
    run(server.connect_host("WEB-1"))
    assert cb.connect_calls == ["web-1"]


def test_unknown_host_opens_nothing(cb):
    with pytest.raises(RuntimeError, match="no saved host"):
        run(server.connect_host("203.0.113.9"))
    assert cb.connect_calls == [] and len(cb.terms) == 1


def test_dry_run_connects_to_nothing(cb):
    out = run(server.connect_host("web-1", dry_run=True))
    assert "dry run ok" in out
    assert cb.connect_calls == [] and len(cb.terms) == 1


def test_allowlist_blocks_other_hosts_before_anything_happens(cb, monkeypatch):
    monkeypatch.setenv("TERMIUS_CONNECT_ALLOW", "db-1, staging-1")
    with pytest.raises(RuntimeError, match="TERMIUS_CONNECT_ALLOW"):
        run(server.connect_host("web-1"))
    assert cb.connect_calls == []
    run(server.connect_host("db-1"))
    assert cb.connect_calls == ["db-1"]


def test_several_windows_refuse_to_open(monkeypatch, lockdir):
    b = ConnectBridge([FakeTerm("aaaa1111-0000-0000-0000-000000000001")], HOSTS, windows=2)
    monkeypatch.setattr(server, "Page", lambda: b)
    with pytest.raises(RuntimeError, match="Nothing was opened"):
        run(server.connect_host("web-1"))
    assert b.connect_calls == []


def test_tab_that_never_connects_is_claimed_but_reported(cb, monkeypatch):
    cb.delay_reads = 10 ** 6
    out = run(server.connect_host("web-1", wait=1))
    assert "NOT connected" in out and "do not guess" in out
    assert server._mine(server._read_claim(new_tab(cb).id))


def test_no_new_tab_means_nothing_is_claimed(cb):
    cb.connects = False
    with pytest.raises(RuntimeError, match="no new tab appeared"):
        run(server.connect_host("web-1", wait=1))
    assert server._read_claim("aaaa1111-0000-0000-0000-000000000001") is None


def test_ambiguous_new_tabs_are_not_claimed(cb):
    cb.extra_tabs = 2
    with pytest.raises(RuntimeError, match="several new tabs"):
        run(server.connect_host("web-1"))
    assert all(server._read_claim(t.id) is None for t in cb.terms.values())


def test_new_tab_never_claims_a_tab_the_human_opened_earlier(monkeypatch, lockdir):
    old = FakeTerm("dddd0000-0000-0000-0000-000000000009", name="web-1", protocol="ssh")
    b = ConnectBridge([FakeTerm("aaaa1111-0000-0000-0000-000000000001"), old], HOSTS)
    b._pending[old.id] = {"label": "web-1", "address": "10.0.0.1", "reads": 0}
    monkeypatch.setattr(server, "Page", lambda: b)
    monkeypatch.setattr(server.asyncio, "sleep", _fast_sleep)
    run(server.connect_host("web-1", new_tab=True))
    assert server._read_claim(old.id) is None
    assert b.connect_calls == ["web-1"]


@pytest.fixture
def pb(monkeypatch, lockdir):
    b = ConnectBridge([FakeTerm("aaaa1111-0000-0000-0000-000000000001")], HOSTS, prompt="10.0.0.1")
    monkeypatch.setattr(server, "Page", lambda: b)
    monkeypatch.setattr(server.asyncio, "sleep", _fast_sleep)
    monkeypatch.delenv("TERMIUS_CONNECT_ALLOW", raising=False)
    monkeypatch.delenv("TERMIUS_HOSTKEY", raising=False)
    return b


def test_first_time_host_key_is_accepted_and_reported(pb, monkeypatch):
    monkeypatch.setenv("TERMIUS_HOSTKEY", "add")  # explicit opt-in only
    out = run(server.connect_host("web-1", wait=5))
    assert pb.answered == [("10.0.0.1", "add")]
    assert "SHA256:abc" in out and "accepted" in out and "NOT connected" not in out
    assert server._mine(server._read_claim(new_tab(pb).id))


def test_once_mode_is_passed_through(pb, monkeypatch):
    monkeypatch.setenv("TERMIUS_HOSTKEY", "once")
    run(server.connect_host("web-1", wait=5))
    assert pb.answered == [("10.0.0.1", "once")]


@pytest.mark.parametrize("value", ["ask", "bogus", ""])
def test_ask_or_unknown_mode_presses_nothing(pb, monkeypatch, value):
    monkeypatch.setenv("TERMIUS_HOSTKEY", value)
    if value == "":
        monkeypatch.delenv("TERMIUS_HOSTKEY")      # unset = leave prompt to the human
        run(server.connect_host("web-1", wait=5))
        assert pb.answered == []
        return
    out = run(server.connect_host("web-1", wait=1))
    assert pb.answered == [] and "NOT connected" in out


def test_prompt_for_another_address_is_left_alone(pb):
    pb.prompt = "10.0.0.2"                         # a prompt exists, but not for web-1
    out = run(server.connect_host("web-1", wait=1))
    assert pb.answered == [] and "NOT connected" in out


def test_ambiguous_prompts_are_reported_not_answered(pb, monkeypatch):
    monkeypatch.setenv("TERMIUS_HOSTKEY", "add")
    def boom(address, mode):
        raise RuntimeError("2 host-key prompts name 10.0.0.1; nothing was pressed")
    monkeypatch.setattr(pb, "_answerHostKey", boom)
    out = run(server.connect_host("web-1", wait=1))
    assert "left unanswered" in out and "2 host-key prompts" in out


# ---- reuse of an existing tab ------------------------------------------------

def _web_tab(b, tid, connected=True):
    t = FakeTerm(tid, name="web-1", protocol="ssh", prompt="u@h $")
    b.terms[t.id] = t
    if not connected:
        b._pending[t.id] = {"label": "web-1", "address": "10.0.0.1", "reads": 0}
        b.delay_reads = 10 ** 6
    return t


def _mark_host(b, t, agent=True):
    """FakeBridge reports `host` from _pending; give a pre-existing tab the same. By default
    it counts as opened by THIS session (the registry is what makes a tab reusable)."""
    b._pending[t.id] = {"label": "web-1", "address": "10.0.0.1", "reads": b.delay_reads}
    if agent:
        server._mark_opened(t.id)


def test_reuses_an_unclaimed_connected_tab_instead_of_opening(cb):
    t = _web_tab(cb, "dddd0001-0000-0000-0000-000000000001")
    _mark_host(cb, t)
    out = run(server.connect_host("web-1"))
    assert cb.connect_calls == [] and len(cb.terms) == 2
    assert "reused existing tab dddd0001" in out
    assert server._mine(server._read_claim(t.id))


def test_new_tab_flag_skips_reuse(cb):
    t = _web_tab(cb, "dddd0001-0000-0000-0000-000000000001")
    _mark_host(cb, t)
    run(server.connect_host("web-1", new_tab=True))
    assert cb.connect_calls == ["web-1"] and len(cb.terms) == 3
    assert server._read_claim(t.id) is None            # the old tab was not touched


def test_tab_claimed_by_another_session_is_never_taken_or_duplicated(cb, monkeypatch):
    t = _web_tab(cb, "dddd0001-0000-0000-0000-000000000001")
    _mark_host(cb, t)
    with monkeypatch.context() as m:                    # write the claim as another session
        m.setattr(server, "OWNER", dict(server.OWNER, token="someone-else"))
        server._write_claim(t.id)
    with pytest.raises(RuntimeError, match="claimed by another session"):
        run(server.connect_host("web-1"))
    assert cb.connect_calls == []
    assert not server._mine(server._read_claim(t.id))


def test_several_free_tabs_are_ambiguous(cb):
    for i in (1, 2):
        _mark_host(cb, _web_tab(cb, f"dddd000{i}-0000-0000-0000-000000000001"))
    with pytest.raises(RuntimeError, match="2 tabs of web-1 are available"):
        run(server.connect_host("web-1"))
    assert cb.connect_calls == []


def test_own_claimed_tab_wins_over_a_free_one(cb):
    a = _web_tab(cb, "dddd0001-0000-0000-0000-000000000001")
    b2 = _web_tab(cb, "dddd0002-0000-0000-0000-000000000001")
    _mark_host(cb, a); _mark_host(cb, b2)
    server._write_claim(b2.id)
    out = run(server.connect_host("web-1"))
    assert "dddd0002" in out and server._read_claim(a.id) is None


def test_disconnected_tab_is_not_reused(cb):
    t = _web_tab(cb, "dddd0001-0000-0000-0000-000000000001", connected=False)
    _mark_host(cb, t)
    cb.delay_reads = 0
    t2 = cb.terms[t.id]
    cb._pending[t.id]["reads"] = -10 ** 6               # stays "not connected"
    cb.delay_reads = 0
    run(server.connect_host("web-1"))
    assert cb.connect_calls == ["web-1"]


def test_dry_run_reports_reuse_and_claims_nothing(cb):
    t = _web_tab(cb, "dddd0001-0000-0000-0000-000000000001")
    _mark_host(cb, t)
    out = run(server.connect_host("web-1", dry_run=True))
    assert "would reuse tab dddd0001" in out
    assert server._read_claim(t.id) is None and cb.connect_calls == []


def test_a_tab_a_human_opened_is_not_reused_without_an_explicit_claim(cb):
    t = _web_tab(cb, "dddd0001-0000-0000-0000-000000000001")
    _mark_host(cb, t, agent=False)
    run(server.connect_host("web-1"))
    assert cb.connect_calls == ["web-1"]                # opened its own tab instead
    assert server._read_claim(t.id) is None


def test_opened_tabs_are_registered_and_the_next_call_reuses_them(cb):
    first = run(server.connect_host("web-1"))
    tab = new_tab(cb)
    assert server._read_opened(tab.id)["token"] == server.OWNER["token"]
    server._claim_path(tab.id).unlink()                 # e.g. released, or the claim idled out
    again = run(server.connect_host("web-1"))
    assert cb.connect_calls == ["web-1"]                # no second tab
    assert "reused existing tab" in again and tab.id[:8] in again
    assert server._mine(server._read_claim(tab.id))


def test_list_marks_agent_opened_tabs(cb):
    run(server.connect_host("web-1"))
    out = run(server.list_terminal_sessions())
    assert "opened by agent: THIS session" in out
    assert "opened by agent" not in [l for l in out.splitlines() if "aaaa1111" in l][0]


def test_a_tab_opened_by_another_session_is_listed_with_that_session(cb, monkeypatch):
    t = _web_tab(cb, "dddd0001-0000-0000-0000-000000000001")
    _mark_host(cb, t, agent=False)
    with monkeypatch.context() as m:
        m.setattr(server, "OWNER", dict(server.OWNER, token="x", session="runtime-other"))
        server._mark_opened(t.id)
    assert "opened by agent: session runtime-other" in run(server.list_terminal_sessions())


def test_a_tab_another_session_opened_is_not_reused(cb, monkeypatch):
    t = _web_tab(cb, "dddd0001-0000-0000-0000-000000000001")
    _mark_host(cb, t, agent=False)
    with monkeypatch.context() as m:
        m.setattr(server, "OWNER", dict(server.OWNER, token="x", session="runtime-other"))
        server._mark_opened(t.id)                       # unclaimed, opened by someone else
    run(server.connect_host("web-1"))
    assert cb.connect_calls == ["web-1"]                # own new tab, theirs untouched
    assert server._read_claim(t.id) is None
