"""Regressions from the local audit; never connect to Termius or SSH."""
import pytest

import server
from conftest import run
from test_multi_window import serve, target


def test_prompt_equal_output_is_not_completion(bridge, term):
    original = term.input

    def submit(data):
        if data == "\r":
            term.sent.append(data)
            term.lines.append(term.prompt)
            term.busy = True
        else:
            original(data)

    term.input = submit
    run(server.claim_terminal(term.id))
    out = run(server.run_command("long-job", term.id, timeout=1))
    assert "status: timeout" in out
    assert term.prompt in out.splitlines()[2:]


@pytest.mark.parametrize("tool", ["send_text", "run_command"])
@pytest.mark.parametrize("text", ["echo hi\x0f", "ls\x1b[A", "x\x7f", "a\u0085b"])
def test_control_characters_cannot_bypass_input_tool(bridge, term, tool, text):
    run(server.claim_terminal(term.id))
    with pytest.raises(ValueError, match="control"):
        run(getattr(server, tool)(text, term.id))
    assert term.sent == []


def test_execution_capable_tools_have_destructive_annotations():
    tools = {t.name: t for t in run(server.mcp.list_tools())}
    for name in ("send_text", "send_key", "run_command"):
        assert tools[name].annotations.readOnlyHint is False
        assert tools[name].annotations.destructiveHint is True


def test_default_hostkey_mode_never_auto_accepts(monkeypatch):
    monkeypatch.delenv("TERMIUS_HOSTKEY", raising=False)
    assert server._hostkey_mode() == "ask"


def test_success_reports_prompt_observation_not_exit_status(bridge, term):
    term.outputs["false"] = ["diagnostic"]
    run(server.claim_terminal(term.id))
    out = run(server.run_command("false", term.id, timeout=5))
    assert "status: prompt-observed" in out
    assert "exit code unknown" in out
    assert "diagnostic" in out


def test_trimmed_capture_returns_retained_output_and_loss_notice(bridge, term, monkeypatch):
    term.outputs["large-job"] = ["RESULT_RETAINED"]
    run(server.claim_terminal(term.id))
    original = bridge.call

    async def call(fn, *args):
        if fn == "readCapture":
            data = bridge._read(term.id, len(term.lines) - 2)
            return {**data, "history_lost": True}
        return await original(fn, *args)

    monkeypatch.setattr(bridge, "call", call)
    out = run(server.run_command("large-job", term.id, timeout=5))
    assert "RESULT_RETAINED" in out
    assert "history-lost" in out


def test_enter_not_sent_if_marker_tracking_unavailable(bridge, term, monkeypatch):
    run(server.claim_terminal(term.id))
    original = bridge.call

    async def call(fn, *args):
        if fn == "startCapture":
            raise RuntimeError("marker tracking unavailable")
        return await original(fn, *args)

    monkeypatch.setattr(bridge, "call", call)
    with pytest.raises(RuntimeError, match="marker tracking unavailable"):
        run(server.run_command("echo hi", term.id))
    assert term.sent == []


@pytest.mark.parametrize("url", ["ws://example.com:9222/a", "ws://127.0.0.1:8080/a", "ws://user:pw@127.0.0.1:9222/a"])
def test_cdp_target_cannot_redirect_to_another_endpoint(monkeypatch, url):
    item = target(1)
    item["webSocketDebuggerUrl"] = url
    serve(monkeypatch, [item])
    with pytest.raises(RuntimeError, match="loopback"):
        server._target_ws()


def test_capture_disposed_after_observation_failure(bridge, term, monkeypatch):
    run(server.claim_terminal(term.id))
    original = bridge.call
    def fail(data):
        term.sent.append(data)
        if data != "\r":
            term.lines[-1] += data
    term.input = fail

    async def call(fn, *args):
        if fn == "readCapture":
            raise RuntimeError("renderer unavailable")
        return await original(fn, *args)

    monkeypatch.setattr(bridge, "call", call)
    out = run(server.run_command("long-job", term.id, timeout=1))
    assert "observation-lost" in out and "do not automatically retry" in out
    assert bridge.captures == {}


def test_lost_enter_ack_cannot_be_reported_as_safe_to_retry(bridge, term, monkeypatch):
    run(server.claim_terminal(term.id))
    original = bridge.call
    async def call(fn, *args):
        result = await original(fn, *args)
        if fn == "input" and args[1] == "\r":
            raise TimeoutError("ack lost")
        return result
    monkeypatch.setattr(bridge, "call", call)
    with pytest.raises(RuntimeError, match="do not automatically retry"):
        run(server.run_command("echo hi", term.id))
    assert term.sent == ["echo hi", "\r"]
