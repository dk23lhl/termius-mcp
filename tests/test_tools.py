"""The tools end to end against a fake Termius: what gets typed, and what is refused."""
import pytest

import server
from conftest import foreign_claim, run

LOCAL = "Local Terminal"
WEB = "web-1"


def claim(name=LOCAL):
    return run(server.claim_terminal(name))


# ---- input is refused unless claimed -------------------------------------

def test_run_command_without_claim_sends_nothing(bridge, term):
    with pytest.raises(RuntimeError, match="claim_terminal"):
        run(server.run_command("echo hi", LOCAL))
    assert term.sent == []


def test_send_key_and_send_text_without_claim_send_nothing(bridge, term):
    with pytest.raises(RuntimeError, match="not claimed"):
        run(server.send_key("ctrl-c", LOCAL))
    with pytest.raises(RuntimeError, match="not claimed"):
        run(server.send_text("ls", LOCAL))
    assert term.sent == []


def test_tab_claimed_by_another_session_is_refused(bridge, term):
    foreign_claim(term.id)
    with pytest.raises(RuntimeError, match="claimed by session runtime-other"):
        run(server.run_command("echo hi", LOCAL))
    with pytest.raises(RuntimeError, match="already claimed"):
        claim()
    assert term.sent == []


def test_claim_is_per_tab(bridge, term):
    claim(LOCAL)
    with pytest.raises(RuntimeError, match="not claimed"):
        run(server.send_key("enter", WEB))
    assert bridge.terms[bridge.terms and next(i for i in bridge.terms if i.startswith("bbbb"))].sent == []


def test_release_then_input_is_refused_again(bridge, term):
    claim()
    run(server.release_terminal(LOCAL))
    with pytest.raises(RuntimeError, match="not claimed"):
        run(server.send_key("enter", LOCAL))


def test_cannot_release_someone_elses_claim(bridge, term):
    foreign_claim(term.id)
    with pytest.raises(RuntimeError, match="only its owner can release"):
        run(server.release_terminal(LOCAL))
    assert server._read_claim(term.id) is not None


def test_claim_is_idempotent(bridge):
    claim()
    claim()


def test_claim_listing_shows_owner(bridge, term):
    claim()
    foreign_claim(bridge.terms[next(i for i in bridge.terms if i.startswith("bbbb"))].id)
    out = run(server.list_terminal_sessions())
    assert "claimed by THIS session" in out
    assert "claimed by session runtime-other" in out


# ---- target selection ------------------------------------------------------

def test_input_must_name_the_tab_when_several_are_open(bridge):
    with pytest.raises(RuntimeError, match="Several sessions are open"):
        run(server.claim_terminal(None))


def test_reads_fall_back_to_the_active_tab(bridge):
    assert run(server.read_terminal()).startswith(f"[{LOCAL}]")


def test_session_matches_by_id_prefix_and_ignores_case(bridge):
    assert run(server.read_terminal("AAAA1111")).startswith(f"[{LOCAL}]")
    assert run(server.read_terminal("WEB-1")).startswith(f"[{WEB}]")


def test_ambiguous_or_unknown_session_is_rejected(bridge):
    with pytest.raises(RuntimeError, match="matches no session"):
        run(server.read_terminal("nope"))


# ---- run_command ----------------------------------------------------------

def test_run_command_returns_output_without_echo_or_prompt(bridge, term):
    term.outputs["echo hi"] = ["hi"]
    claim()
    out = run(server.run_command("echo hi", LOCAL, timeout=5))
    assert out == f"[{LOCAL}] $ echo hi\n[status: prompt-observed — heuristic only; exit code unknown]\nhi"
    assert term.sent == ["echo hi", "\r"]


def test_run_command_reports_no_output(bridge, term):
    claim()
    assert run(server.run_command("true", LOCAL, timeout=5)).endswith("(no output)")


def test_run_command_refuses_while_another_command_is_running(bridge, term):
    term.busy = True
    claim()
    with pytest.raises(RuntimeError, match="not at a shell prompt"):
        run(server.run_command("echo hi", LOCAL))
    assert term.sent == []          # nothing was typed into the running program


def test_run_command_refuses_a_progress_line_that_ends_in_percent(bridge, term):
    """Regression: a running program's last line ('45%') used to pass as a prompt."""
    term.lines[-1] = "Downloading... 45%"
    claim()
    with pytest.raises(RuntimeError, match="not at a shell prompt"):
        run(server.run_command("echo hi", LOCAL))
    assert term.sent == []


def test_run_command_refuses_while_the_screen_is_still_changing(bridge, term):
    """Even if the cursor line looks like a prompt (with trailing space), a screen that
    keeps changing means a program is still drawing."""
    n = {"i": 0}

    def redraw(t):
        n["i"] += 1
        t.lines[-1] = f"progress {n['i']}% "
    term.on_read = redraw
    claim()
    with pytest.raises(RuntimeError, match="still changing"):
        run(server.run_command("echo hi", LOCAL))
    assert term.sent == []


def test_run_command_does_not_call_a_stalled_progress_line_done(bridge, term):
    """Regression (found by the live smoke test): a program idling on 'Downloading... 45%'
    for over a second was reported as finished, with its output dropped."""
    state = {"reads": None}
    orig_input = term.input

    def start_download(data):
        if data == "\r":
            term.lines.append("Downloading... 45%")       # running: no new prompt yet
            state["reads"] = 0
        else:
            orig_input(data)
    term.input = start_download

    def tick(t):
        if state["reads"] is not None:
            state["reads"] += 1
            if state["reads"] == 12:                         # ~3 s later the program exits
                t.lines.append("finished")
                t.lines.append("me@box % ")
    term.on_read = tick
    claim()
    out = run(server.run_command("dl", LOCAL, timeout=10))
    assert "status: prompt-observed" in out
    assert out.splitlines()[-2:] == ["Downloading... 45%", "finished"]


def test_run_command_refuses_when_text_is_already_typed(bridge, term):
    term.lines[-1] = "me@box % rm -rf /tm"
    claim()
    with pytest.raises(RuntimeError, match="not at a shell prompt"):
        run(server.run_command("echo hi", LOCAL))
    assert term.sent == []


def test_run_command_rejects_multiline_commands(bridge, term):
    claim()
    for bad in ("echo a\necho b", "echo a\recho b"):
        with pytest.raises(ValueError, match="one command per call"):
            run(server.run_command(bad, LOCAL))
    assert term.sent == []


def test_run_command_refuses_disconnected_tab(bridge, term, monkeypatch):
    claim()
    orig = bridge._sessions
    monkeypatch.setattr(bridge, "_sessions",
                        lambda: [{**s, "connected": False} for s in orig()])
    with pytest.raises(RuntimeError, match="disconnected"):
        run(server.run_command("echo hi", LOCAL))
    assert term.sent == []


def test_run_command_while_tab_lock_is_held_sends_nothing(bridge, term):
    claim()

    async def go():
        async with server._tab_lock({"id": term.id, "name": LOCAL}):
            await server.run_command("echo hi", LOCAL)
    with pytest.raises(RuntimeError, match="busy"):
        run(go())
    assert term.sent == []


def test_finishing_a_command_never_steals_a_claim_taken_over_meanwhile(bridge, term):
    """If our claim lapsed during a long command and another session claimed the tab,
    the post-command refresh must leave their claim alone."""
    claim()
    orig_input = term.input

    def input_then_lose_claim(data):
        orig_input(data)
        if data == "\r":
            foreign_claim(term.id)
    term.input = input_then_lose_claim
    run(server.run_command("true", LOCAL, timeout=5))
    c = server._read_claim(term.id)
    assert c["session"] == "runtime-other" and not server._mine(c)


# ---- send_text / send_key ---------------------------------------------------

def test_send_text_rejects_newlines(bridge, term):
    claim()
    for bad in ("a\nb", "a\rb"):
        with pytest.raises(ValueError, match="newlines"):
            run(server.send_text(bad, LOCAL))
    assert term.sent == []


def test_send_text_types_without_running(bridge, term):
    claim()
    run(server.send_text("ls -la", LOCAL))
    assert term.sent == ["ls -la"] and "\r" not in term.sent


def test_send_key_rejects_unknown_key(bridge, term):
    claim()
    with pytest.raises(ValueError, match="unknown key"):
        run(server.send_key("ctrl-x", LOCAL))
    assert term.sent == []


def test_send_key_sends_the_control_sequence(bridge, term):
    claim()
    run(server.send_key("CTRL-C", LOCAL))
    assert term.sent == ["\x03"]


# ---- read_terminal ----------------------------------------------------------

def test_read_terminal_caps_long_output(bridge, term):
    term.lines = [f"line {i}" for i in range(1000)] + ["me@box % "]
    out = run(server.read_terminal(LOCAL, scrollback=True))
    assert "lines omitted" in out and out.count("\n") <= server.MAX_LINES + 2


def test_read_terminal_needs_no_claim(bridge):
    assert run(server.read_terminal(LOCAL))
