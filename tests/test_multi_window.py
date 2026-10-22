"""Multi-window behaviour: with several Termius main windows only one renderer is
reachable, so anything that types must fail closed and reads must say they are partial."""
import json

import pytest

import server
from conftest import FakeBridge, FakeTerm, run


class FakeResponse:
    def __init__(self, targets):
        self._b = json.dumps(targets).encode()

    def read(self, *a):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def target(n, kind="primary", origin=server.TERMIUS_ORIGIN):
    return {"url": f"{origin}main.html?type={kind}&id={n}",
            "webSocketDebuggerUrl": f"ws://127.0.0.1:9222/devtools/page/{n}"}


def serve(monkeypatch, targets):
    monkeypatch.setattr(server.urllib.request, "urlopen", lambda *a, **k: FakeResponse(targets))


def test_single_main_window(monkeypatch):
    serve(monkeypatch, [target(3), target(2, "preferences")])
    assert server._target_ws() == ("ws://127.0.0.1:9222/devtools/page/3", 1)


def test_preferences_and_background_windows_are_not_counted(monkeypatch):
    serve(monkeypatch, [target(2, "preferences"), target(3), target(9, "background")])
    assert server._target_ws()[1] == 1


def test_window_choice_is_deterministic_whatever_the_list_order(monkeypatch):
    serve(monkeypatch, [target(7), target(3)])
    first = server._target_ws()
    serve(monkeypatch, [target(3), target(7)])
    assert server._target_ws() == first and first[1] == 2


def test_foreign_page_on_the_port_is_ignored(monkeypatch):
    serve(monkeypatch, [target(3, origin="file:///Applications/Other.app/")])
    with pytest.raises(RuntimeError, match="no Termius main window"):
        server._target_ws()


def test_closed_port_gives_actionable_error(monkeypatch):
    def boom(*a, **k):
        raise OSError("refused")
    monkeypatch.setattr(server.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError, match="remote-debugging-port"):
        server._target_ws()


@pytest.fixture
def two_windows(monkeypatch, lockdir):
    t = FakeTerm("aaaa1111-0000-0000-0000-000000000001")
    b = FakeBridge([t], windows=2)
    monkeypatch.setattr(server, "Page", lambda: b)
    return t


def test_claim_is_refused_when_several_windows_are_open(two_windows):
    with pytest.raises(RuntimeError, match="2 Termius main windows"):
        run(server.claim_terminal("Local Terminal"))


def test_input_is_refused_even_with_a_prior_claim(two_windows):
    server._write_claim(two_windows.id)        # claim made while one window was open
    for call in (lambda: server.run_command("echo hi", "Local Terminal"),
                 lambda: server.send_key("enter", "Local Terminal"),
                 lambda: server.send_text("ls", "Local Terminal")):
        with pytest.raises(RuntimeError, match="Nothing was sent"):
            run(call())
    assert two_windows.sent == []


def test_reads_still_work_but_warn_they_are_partial(two_windows):
    assert "WARNING: 2 Termius main windows" in run(server.read_terminal("Local Terminal"))
    assert "WARNING: 2 Termius main windows" in run(server.list_terminal_sessions())


def test_no_warning_with_one_window(monkeypatch, lockdir):
    t = FakeTerm("aaaa1111-0000-0000-0000-000000000001")
    monkeypatch.setattr(server, "Page", lambda: FakeBridge([t]))
    assert "WARNING" not in run(server.list_terminal_sessions())


# ---- running-version check ---------------------------------------------------

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Termius/{} Chrome/106.0.5249.199 Electron/21.4.4 Safari/537.36")


class Connected(Exception):
    """Raised instead of opening the websocket: the checks before it passed."""


def cdp(monkeypatch, user_agent):
    pages = {"/json/version": {"Browser": "Chrome/106.0.5249.199", "User-Agent": user_agent},
             "/json/list": [target(3)]}

    def urlopen(url, *a, **k):
        return FakeResponse(next(v for p, v in pages.items() if url.endswith(p)))

    async def connect(*a, **k):
        raise Connected()
    monkeypatch.setattr(server.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(server.websockets, "connect", connect)


def test_supported_running_version_proceeds_to_connect(monkeypatch):
    cdp(monkeypatch, UA.format("9.38.1"))
    with pytest.raises(Connected):
        run(server.Page().__aenter__())


@pytest.mark.parametrize("ua, message", [
    (UA.format("9.39.0"), "running Termius reports 9.39.0"),
    (UA.format("9.38.10"), "running Termius reports 9.38.10"),
    ("Mozilla/5.0 HeadlessChrome/120.0.0.0 Safari/537.36", "does not report a Termius"),
])
def test_other_running_versions_are_refused_before_any_connection(monkeypatch, ua, message):
    """The running app decides, not the bundle on disk (which may already be updated)."""
    cdp(monkeypatch, ua)
    with pytest.raises(RuntimeError, match=message):
        run(server.Page().__aenter__())
