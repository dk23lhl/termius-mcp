"""Regressions from the local audit; never connect to Termius or SSH."""
import time

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


def test_enter_waits_for_a_slow_remote_echo(bridge, term):
    """On a slow link the screen stays unchanged until the echo arrives; Enter used to
    go out ~0.2 s after typing, ahead of the echo."""
    term.outputs["echo hi"] = ["hi"]
    original, late, early = term.input, [], []

    def remote(data):
        if data == "\r" and late:
            early.append(data)
        if data not in ("\r", "\x15"):
            term.sent.append(data)
            late.append([data, 4])              # the echo shows up on the 4th read
            return
        original(data)

    def arrive(t):
        if late:
            late[0][1] -= 1
            if late[0][1] <= 0:
                t.lines[-1] += late.pop()[0]

    term.input, term.on_read = remote, arrive
    run(server.claim_terminal(term.id))
    out = run(server.run_command("echo hi", term.id, timeout=5))
    assert early == [], "Enter was sent before the remote echo appeared"
    assert term.sent == ["echo hi", "\r"]
    assert out.splitlines()[-1] == "hi"


def test_resize_is_reported_without_claiming_history_loss(bridge, term, monkeypatch):
    term.outputs["job"] = ["RESULT"]
    run(server.claim_terminal(term.id))
    original = bridge.call

    async def call(fn, *args):
        data = await original(fn, *args)
        return {**data, "resized": True} if fn == "readCapture" else data

    monkeypatch.setattr(bridge, "call", call)
    out = run(server.run_command("job", term.id, timeout=5))
    assert "terminal resized" in out and "history-lost" not in out
    assert out.splitlines()[-1] == "RESULT"


def _submit_shows(term, line, busy):
    original = term.input

    def submit(data):
        if data == "\r":
            term.sent.append(data)
            term.lines.append(line)
            term.busy = busy            # busy: the cursor moved on to an empty row
        else:
            original(data)
    term.input = submit


def test_a_heading_ending_in_colon_is_not_waiting_for_input(bridge, term):
    """'Results:' then a silent computation: the cursor is on the next row, so the
    program is working, not asking. It used to return waiting-for-input after ~2 s."""
    _submit_shows(term, "Results:", busy=True)
    run(server.claim_terminal(term.id))
    out = run(server.run_command("report", term.id, timeout=3))
    assert "status: timeout" in out and "waiting-for-input" not in out


@pytest.mark.parametrize("question", ["Password: ", "Proceed? [Y/n] ", "Continue? "])
def test_cursor_after_a_question_is_waiting_for_input(bridge, term, question):
    _submit_shows(term, question, busy=False)
    run(server.claim_terminal(term.id))
    out = run(server.run_command("ask", term.id, timeout=4))
    assert "status: waiting-for-input" in out


def test_unlocated_anchor_after_resize_is_reported(bridge, term, monkeypatch):
    term.outputs["job"] = ["RESULT"]
    run(server.claim_terminal(term.id))
    original = bridge.call

    async def call(fn, *args):
        data = await original(fn, *args)
        if fn == "readCapture":
            return {**data, "resized": True, "anchor_lost": True}
        return data

    monkeypatch.setattr(bridge, "call", call)
    out = run(server.run_command("job", term.id, timeout=5))
    assert "output-boundary-unknown" in out and "history-lost" not in out


def _advertised_tools(keychain_env):
    import json, os, subprocess, sys
    from pathlib import Path
    env = {k: v for k, v in os.environ.items() if k != "TERMIUS_ENABLE_KEYCHAIN"}
    if keychain_env is not None:
        env["TERMIUS_ENABLE_KEYCHAIN"] = keychain_env
    code = ("import asyncio, json, server; "
            "print(json.dumps(sorted(t.name for t in asyncio.run(server.mcp.list_tools()))))")
    res = subprocess.run([sys.executable, "-W", "ignore", "-c", code], env=env, timeout=60,
                         cwd=Path(server.__file__).parent, capture_output=True, text=True, check=True)
    return json.loads(res.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("value", [None, "", "0", "yes"])
def test_list_keychain_is_not_advertised_unless_enabled(value):
    tools = _advertised_tools(value)
    assert "list_keychain" not in tools and len(tools) == 14


def test_list_keychain_is_advertised_when_enabled():
    tools = _advertised_tools("1")
    assert "list_keychain" in tools and len(tools) == 15


def test_echo_is_seen_by_its_text_when_the_cursor_does_not_move(bridge, term, monkeypatch):
    """Real xterm, full scrollback: an echo that fills whole rows scrolls the top row off
    and leaves the absolute cursor where it was (see capture.test.js). The echo used to
    count as never arriving: Ctrl-U, then refusal of a command that was on screen."""
    term.outputs["echo hi"] = ["hi"]
    original, pinned = bridge._read, {}

    def read(tid, start=None):
        r = original(tid, start)
        if "\r" not in term.sent:          # until Enter the cursor appears not to move
            pos = pinned.setdefault("pos", (r["cursor_row"], r["cursor_x"]))
            r["cursor_row"], r["cursor_x"] = pos
        return r

    monkeypatch.setattr(bridge, "_read", read)
    run(server.claim_terminal(term.id))
    out = run(server.run_command("echo hi", term.id, timeout=5))
    assert term.sent == ["echo hi", "\r"]           # no Ctrl-U
    assert "status: prompt-observed" in out and out.splitlines()[-1] == "hi"


def test_ambiguous_anchor_after_resize_is_reported(bridge, term, monkeypatch):
    term.outputs["job"] = ["RESULT"]
    run(server.claim_terminal(term.id))
    original = bridge.call

    async def call(fn, *args):
        data = await original(fn, *args)
        if fn == "readCapture":
            return {**data, "resized": True, "anchor_lost": True, "anchor_reason": "ambiguous"}
        return data

    monkeypatch.setattr(bridge, "call", call)
    out = run(server.run_command("job", term.id, timeout=5))
    assert "output-boundary-unknown: after the resize the command line appeared more than once" in out


# ---- capture transfer and quiet windows ------------------------------------------

def _cap_like_renderer(lines, head, tail):
    """Python mirror of bridge.js capLines."""
    n = len(lines)
    while n and not lines[n - 1].rstrip():
        n -= 1
    if n <= head + tail:
        return {"lines": lines[:n], "head": n, "omitted": 0}
    return {"lines": lines[:head] + lines[n - tail:n], "head": head, "omitted": n - head - tail}


@pytest.mark.parametrize("n", [0, 1, 150, 199, 200, 201, 202, 203, 250, 351, 1000])
@pytest.mark.parametrize("prompt_dropped", [True, False])
def test_split_cap_gives_the_same_text_as_the_full_list(n, prompt_dropped):
    full = [f"L{i}" for i in range(n)] + ["me@box % "]
    got = _cap_like_renderer(full, server.CAPTURE_HEAD, server.CAPTURE_TAIL)
    lines, head = got["lines"], got["head"]
    if prompt_dropped:
        full, lines, head = full[:-1], lines[:-1], min(head, len(lines) - 1)
    assert server._render(lines, head, got["omitted"], server.MAX_LINES) == server._cap(full, server.MAX_LINES)


def test_capped_transfer_returns_the_same_output_as_a_full_one(bridge, term, monkeypatch):
    term.outputs["seq 500"] = [str(i) for i in range(500)]
    run(server.claim_terminal(term.id))
    full = run(server.run_command("seq 500", term.id, timeout=5))
    original = bridge.call

    async def capped(fn, *args):
        data = await original(fn, *args)
        if fn == "readCapture":
            _, _, _, head, tail = args
            return {**data, **_cap_like_renderer(data["lines"], head, tail)}
        return data

    monkeypatch.setattr(bridge, "call", capped)
    assert run(server.run_command("seq 500", term.id, timeout=5)) == full
    assert "lines omitted" in full


def test_unchanged_reads_reuse_the_last_full_read(bridge, term, monkeypatch):
    """With a write counter the renderer answers a known sig with {"same": true}: the
    quiet window still runs on the last full read."""
    term.outputs["echo hi"] = ["hi"]
    run(server.claim_terminal(term.id))
    original, seen = bridge.call, []

    async def call(fn, *args):
        if fn != "readCapture":
            return await original(fn, *args)
        seen.append(args[2])
        if args[2] == "s1":
            return {"same": True, "sig": "s1"}
        return {**(await original(fn, *args)), "sig": "s1"}

    monkeypatch.setattr(bridge, "call", call)
    out = run(server.run_command("echo hi", term.id, timeout=5))
    assert "status: prompt-observed" in out and out.splitlines()[-1] == "hi"
    assert seen[0] is None and seen.count("s1") >= 3      # one full read, then "same" only


def _timed_submit(term):
    original, at = term.input, {}

    def submit(data):
        if data == "\r":
            at["enter"] = time.monotonic()
        original(data)
    term.input = submit
    return at


def test_prompt_quiet_window_is_not_shortened(bridge, term):
    term.outputs["echo hi"] = ["hi"]
    at = _timed_submit(term)
    run(server.claim_terminal(term.id))
    out = run(server.run_command("echo hi", term.id, timeout=5))
    elapsed = time.monotonic() - at["enter"]
    assert "status: prompt-observed" in out
    assert server.PROMPT_QUIET <= elapsed < server.PROMPT_QUIET + 0.6


def test_input_quiet_window_is_not_shortened(bridge, term):
    _submit_shows(term, "Password: ", busy=False)
    at = _timed_submit(term)
    run(server.claim_terminal(term.id))
    out = run(server.run_command("ask", term.id, timeout=5))
    assert "status: waiting-for-input" in out
    assert time.monotonic() - at["enter"] >= server.INPUT_QUIET


@pytest.mark.parametrize("tail,timeout", [("me@box % ", 2), ("Continue? ", 3)])
def test_changes_in_omitted_lines_keep_the_command_running(bridge, term, monkeypatch, tail, timeout):
    """The renderer detects changes to the full text before clipping its transfer.
    Identical transferred ends must not turn changing middle output into a quiet screen."""
    run(server.claim_terminal(term.id))
    original, revisions = bridge.call, []

    async def call(fn, *args):
        if fn != "readCapture":
            return await original(fn, *args)
        revisions.append(len(revisions) + 1)
        return {"lines": ["HEAD", "TAIL", tail], "head": 1, "omitted": 300,
                "prompt_line": tail, "before_cursor": tail, "cursor_row": 303,
                "cursor_x": len(tail), "output_revision": revisions[-1],
                "sig": str(revisions[-1]), "history_lost": False}

    monkeypatch.setattr(bridge, "call", call)
    out = run(server.run_command("progress", term.id, timeout=timeout))
    assert "status: timeout" in out
    assert len(revisions) > 2


def test_writes_without_rendered_changes_do_not_reset_quiet_time(bridge, term, monkeypatch):
    """Keep the original unchanged-screen semantics, rather than counting raw writes."""
    term.outputs["echo hi"] = ["hi"]
    run(server.claim_terminal(term.id))
    original, reads = bridge.call, []

    async def call(fn, *args):
        data = await original(fn, *args)
        if fn == "readCapture":
            reads.append(1)
            return {**data, "sig": str(len(reads)), "output_revision": 1}
        return data

    monkeypatch.setattr(bridge, "call", call)
    out = run(server.run_command("echo hi", term.id, timeout=2))
    assert "status: prompt-observed" in out
    assert out.splitlines()[-1] == "hi"
