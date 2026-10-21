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
