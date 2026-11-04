"""Multi-window behaviour: with several Termius main windows only one renderer is
reachable, so anything that types must fail closed and reads must say they are partial."""
import asyncio
import http.server
import json
import threading

import pytest
from websockets.asyncio.server import serve as ws_serve

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


class FakeOpener:
    def __init__(self, open_url):
        self.open = open_url


def target(n, kind="primary", origin=server.TERMIUS_ORIGIN):
    return {"url": f"{origin}main.html?type={kind}&id={n}",
            "webSocketDebuggerUrl": f"ws://127.0.0.1:9222/devtools/page/{n}"}


def serve(monkeypatch, targets):
    monkeypatch.setattr(server, "LOOPBACK", FakeOpener(lambda *a, **k: FakeResponse(targets)))


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
    monkeypatch.setattr(server, "LOOPBACK", FakeOpener(boom))
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
    monkeypatch.setattr(server, "LOOPBACK", FakeOpener(urlopen))
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


# ---- the debug port never goes through a proxy ------------------------------

@pytest.fixture
def proxied_cdp(monkeypatch):
    """A fake Termius debug port (HTTP and websocket on one port) and a fake proxy, both
    real local servers, with HTTP_PROXY set and NO_PROXY not covering loopback."""
    seen = []

    class Proxy(http.server.BaseHTTPRequestHandler):
        def _refuse(self):
            seen.append(self.requestline)
            self.send_error(502)
        do_GET = do_CONNECT = _refuse

        def log_message(self, *a):
            pass

    proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    ready, state = threading.Event(), {}

    async def answer(ws):
        async for raw in ws:
            state["evaluated"] = state.get("evaluated", 0) + 1
            reply = {"id": json.loads(raw)["id"], "result": {"result": {"value": True}}}
            await ws.send(json.dumps(reply))

    def http_pages(connection, request):
        port = state["port"]
        pages = {"/json/version": {"User-Agent": UA.format("9.38.1")},
                 "/json/list": [{"url": f"{server.TERMIUS_ORIGIN}main.html?type=primary",
                                 "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/page/1"}]}
        if request.path in pages:
            return connection.respond(200, json.dumps(pages[request.path]))
        return None

    async def run():
        state["stop"] = asyncio.get_running_loop().create_future()
        state["loop"] = asyncio.get_running_loop()
        async with ws_serve(answer, "127.0.0.1", 0, process_request=http_pages) as srv:
            state["port"] = srv.sockets[0].getsockname()[1]
            ready.set()
            await state["stop"]

    thread = threading.Thread(target=lambda: asyncio.run(run()), daemon=True)
    thread.start()
    assert ready.wait(5)
    url = f"http://127.0.0.1:{proxy.server_port}"
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(name, url)
    for name in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(server, "PORT", state["port"])
    yield seen, state
    state["loop"].call_soon_threadsafe(state["stop"].set_result, None)
    thread.join(5)
    proxy.shutdown()


def test_cdp_discovery_and_websocket_bypass_a_configured_proxy(proxied_cdp):
    """Regression: with HTTP_PROXY set and NO_PROXY not covering loopback, discovery was
    sent to the proxy and failed as "port is not open", and the CDP websocket, which
    carries terminal text, was tunnelled through it."""
    seen, state = proxied_cdp

    async def go():
        async with server.Page() as page:
            return await page.call("sessions")

    assert run(go()) is True
    assert state["evaluated"] >= 2          # the probe and the call reached the fake port
    assert seen == []
