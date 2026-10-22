"""Pure functions: output cleaning, prompt detection, key table."""
import pytest

import server


def test_clean_strips_ansi_trailing_space_and_collapses_blank_runs():
    raw = ["\x1b[32mok\x1b[0m   ", "", "", "", "next", "", ""]
    assert server._clean(raw) == ["ok", "", "next"]


def test_clean_drops_leading_and_trailing_blank_lines():
    assert server._clean(["", "", "a", "", ""]) == ["a"]


def test_clean_strips_osc_title_sequences():
    assert server._clean(["\x1b]0;title\x07hello"]) == ["hello"]


def test_cap_keeps_short_output_untouched():
    assert server._cap(["a", "b"], 5) == "a\nb"


def test_cap_keeps_head_and_tail_and_reports_omitted_count():
    lines = [str(i) for i in range(100)]
    out = server._cap(lines, 20).splitlines()
    assert out[0] == "0" and out[-1] == "99"
    assert "80 lines omitted" in out[5]
    assert len(out) == 21           # 20 kept + the marker


def test_is_prompt_accepts_common_shell_prompts():
    for p in ("me@box %", "root@host:~#", "user@h ~ $", "❯", "bash-5.2$"):
        assert server._is_prompt(p), p


def test_is_prompt_accepts_oh_my_zsh_only_as_full_line():
    assert server._is_prompt("➜  ~")
    assert server._is_prompt("➜  repo git:(main) ✗")
    # something typed after the prompt means the line is not an empty prompt
    assert not server._is_prompt("➜  repo git:(main) ✗ ls -la")


def test_is_prompt_rejects_running_command_and_blank():
    assert not server._is_prompt("")
    assert not server._is_prompt("Password:")
    assert not server._is_prompt("Downloading file.tar.gz")


@pytest.mark.parametrize("line", ["me@box %", "root@host:~#", "user@h ~ $", "❯", "bash-5.2$"])
def test_at_prompt_accepts_prompt_with_cursor_after_trailing_space(line):
    assert server._at_prompt({"prompt_line": line + " ", "before_cursor": line + " "})


@pytest.mark.parametrize("line", ["Downloading... 45%", "100%", "Progress: 45%", "building #"])
def test_at_prompt_rejects_output_ending_in_a_prompt_symbol(line):
    """Program output ends in the symbol with the cursor right behind it: no space."""
    assert server._is_prompt(line)          # the line alone cannot tell
    assert not server._at_prompt({"prompt_line": line, "before_cursor": line})


def test_at_prompt_rejects_cursor_not_at_end_of_line():
    # text to the right of the cursor (user moved left, or pending input)
    assert not server._at_prompt({"prompt_line": "me@box % ls", "before_cursor": "me@box % "})
    # right side also looks like a prompt: the cursor is still not at the end of the text
    assert not server._at_prompt({"prompt_line": "a $ b $", "before_cursor": "a $ "})
    assert not server._at_prompt({"prompt_line": "me@box %      12:00 $", "before_cursor": "me@box % "})


def test_at_prompt_rejects_typed_text_and_empty_cursor_row():
    assert not server._at_prompt({"prompt_line": "me@box % ls", "before_cursor": "me@box % ls"})
    assert not server._at_prompt({"prompt_line": "", "before_cursor": ""})
    assert not server._at_prompt({"prompt_line": "$", "before_cursor": ""})


def test_at_prompt_handles_prompt_wrapped_over_rows():
    long_prompt = "x" * 150 + " $"
    assert server._at_prompt({"prompt_line": long_prompt + " ", "before_cursor": "xxxx $ "})


def test_at_prompt_needs_the_cursor_fields_from_the_bridge():
    assert not server._at_prompt({"prompt_line": "me@box % "})


def test_key_table_has_expected_control_sequences():
    assert server.KEYS["ctrl-c"] == "\x03"
    assert server.KEYS["enter"] == "\r"
    assert server.KEYS["up"] == "\x1b[A"


def test_every_key_is_a_known_short_sequence():
    assert all(1 <= len(v) <= 3 for v in server.KEYS.values())


# ---- character budget ------------------------------------------------------------

def test_a_long_line_is_clipped_with_its_remaining_length():
    line = "x" * (server.MAX_LINE_CHARS + 500)
    out = server._render([line])
    assert out.startswith("x" * server.MAX_LINE_CHARS)
    assert out.endswith(" … [500 more characters in this line]")


def test_long_lines_keep_the_output_within_the_character_budget():
    """Regression: 200 base64-like lines of 1,000 characters came to about 200,000
    characters, which Claude Code would save to a file instead of showing."""
    lines = [f"{i:03d} " + "A" * 996 for i in range(200)]
    out = server._render(lines)
    assert len(out) <= server.MAX_OUTPUT_CHARS
    shown = out.splitlines()
    note = next(x for x in shown if "lines omitted" in x)
    kept = len(shown) - 1
    assert f"[{200 - kept} lines omitted" in note
    assert shown[0].startswith("000 ") and shown[-1].startswith("199 ")


def test_line_cap_alone_is_unchanged_by_the_character_budget():
    lines = [str(i) for i in range(1000)]
    assert server._render(lines, limit=200) == "\n".join(
        lines[:50] + [server._omitted(800)] + lines[-150:])


def test_split_input_counts_untransferred_lines_in_the_note():
    head = [f"h{i} " + "B" * 900 for i in range(50)]
    tail = [f"t{i} " + "C" * 900 for i in range(150)]
    out = server._render(head + tail, head=50, omitted=1000)
    assert len(out) <= server.MAX_OUTPUT_CHARS
    shown = out.splitlines()
    kept = len(shown) - 1
    assert f"[{1200 - kept} lines omitted" in next(x for x in shown if "omitted" in x)
    assert shown[0].startswith("h0 ") and shown[-1].startswith("t149 ")


def test_host_list_budget_suggests_a_query():
    rows = [f"host-{i}\t" + "d" * 300 for i in range(200)]
    out = server._render(rows, limit=len(rows), hint="pass `query` to narrow the list")
    assert len(out) <= server.MAX_OUTPUT_CHARS and "pass `query` to narrow the list" in out


def _import_with(env):
    import os, subprocess, sys
    full = {k: v for k, v in os.environ.items() if not k.startswith("TERMIUS_MAX_")}
    full.update(env)
    return subprocess.run([sys.executable, "-W", "ignore", "-c",
                           "import server; print(server.MAX_OUTPUT_CHARS, server.MAX_LINE_CHARS)"],
                          cwd=os.path.dirname(server.__file__), env=full,
                          capture_output=True, text=True, timeout=60)


def test_budgets_can_be_raised_by_environment():
    res = _import_with({"TERMIUS_MAX_OUTPUT_CHARS": "80000", "TERMIUS_MAX_LINE_CHARS": "4000"})
    assert res.stdout.split() == ["80000", "4000"]


@pytest.mark.parametrize("env", [{"TERMIUS_MAX_OUTPUT_CHARS": "lots"},
                                 {"TERMIUS_MAX_OUTPUT_CHARS": "100"},
                                 {"TERMIUS_MAX_LINE_CHARS": "20000"}])   # above output // 4
def test_invalid_budgets_stop_the_server_instead_of_being_guessed(env):
    res = _import_with(env)
    assert res.returncode != 0 and "expected an integer" in res.stderr
