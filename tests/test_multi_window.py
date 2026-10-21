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
