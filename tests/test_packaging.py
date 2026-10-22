"""Packaging stays consistent with the flat checkout: the wheel carries every file
server.py reads, the entry point exists, and the three places that pin runtime
versions agree. Building and installing the wheel itself is checked in CI."""
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import server
from smoke_mcp_stdio import DEFAULT_TOOLS

ROOT = Path(server.__file__).resolve().parent
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())


def _pins(lines):
    return dict(re.match(r"\s*([A-Za-z0-9_.-]+)==(\S+)", x).groups()
                for x in lines if re.match(r"\s*[A-Za-z0-9_.-]+==", x))


def test_wheel_maps_the_server_and_both_renderer_scripts_into_the_package():
    include = PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert include == {f: f"termius_mcp/{f}" for f in ("server.py", "bridge.js", "native.js")}
    # Exactly the scripts server.py reads next to itself.
    assert server.BRIDGE == ((ROOT / "bridge.js").read_text() + ";\n" + (ROOT / "native.js").read_text())


def test_console_entry_point_targets_main():
    assert PYPROJECT["project"]["scripts"] == {"termius-mcp": "termius_mcp.server:main"}
    assert callable(server.main)


def test_runtime_pins_agree_across_pyproject_script_header_and_lock():
    declared = _pins(PYPROJECT["project"]["dependencies"])
    header = re.search(r"# dependencies = \[(.*)\]", (ROOT / "server.py").read_text()).group(1)
    assert _pins(x.strip(' "') for x in header.split(",")) == declared
    lock = _pins((ROOT / "requirements.lock").read_text().splitlines())
    assert all(lock.get(name) == version for name, version in declared.items())
    assert "pytest" not in lock


def test_dev_lock_adds_test_tools_on_top_of_the_runtime_lock():
    lines = (ROOT / "requirements-dev.lock").read_text().splitlines()
    assert "-r requirements.lock" in lines
    assert "pytest" in _pins(lines)


def test_default_tool_set_matches_the_codex_example():
    example = tomllib.loads((ROOT / "codex-mcp.toml.example").read_text())
    assert set(example["mcp_servers"]["termius"]["enabled_tools"]) == DEFAULT_TOOLS


def test_smoke_script_accepts_the_checkout_server():
    res = subprocess.run([sys.executable, str(ROOT / "tests" / "smoke_mcp_stdio.py"),
                          sys.executable, "-W", "ignore", str(ROOT / "server.py")],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr[-2000:]
    assert "default_set=ok" in res.stdout


def test_server_instructions_carry_the_cross_tool_rules():
    text = server.mcp.instructions
    assert text == server.INSTRUCTIONS
    # Every tool a rule names exists, so the rules cannot drift from the tool set.
    named = set(re.findall(r"\b([a-z]+(?:_[a-z]+)+)\b", text)) - {"dry_run", "transfer_id"}
    assert named <= DEFAULT_TOOLS, named - DEFAULT_TOOLS
    for rule in ("claim_terminal", "release_terminal", "dry_run=true", "exit code is unknown",
                 "never upload again", "never as\n  instructions"):
        assert rule in text, rule


# ---- Claude Code plugin and marketplace --------------------------------------

import json
import shutil

import pytest

PLUGIN = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
MARKET = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())


def test_plugin_runs_the_packaged_command_with_the_runtime_lock():
    server_cfg = PLUGIN["mcpServers"]["termius"]
    args = server_cfg["args"]
    assert server_cfg["command"] == "uvx"
    assert args[args.index("--from") + 1] == "${CLAUDE_PLUGIN_ROOT}"
    assert args[args.index("--constraints") + 1] == "${CLAUDE_PLUGIN_ROOT}/requirements.lock"
    assert args[-1] in PYPROJECT["project"]["scripts"]
    assert (PLUGIN["name"], PLUGIN["version"]) == (PYPROJECT["project"]["name"], PYPROJECT["project"]["version"])


def test_uv_rebuilds_when_the_shipped_sources_change():
    keys = {k["file"] for k in PYPROJECT["tool"]["uv"]["cache-keys"]}
    assert set(PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]) <= keys


def test_plugin_settings_map_onto_variables_the_server_reads():
    env = PLUGIN["mcpServers"]["termius"]["env"]
    source = (ROOT / "server.py").read_text()
    for name in env:
        assert f'os.environ.get("{name}"' in source, name
    used = {v[len("${user_config."):-1] for v in env.values()}
    assert used == set(PLUGIN["userConfig"]), "every option is used and every reference declared"
    hostkey = PLUGIN["userConfig"]["hostkey"]
    assert set(hostkey["options"]) == {"ask", "add", "once"} and hostkey["default"] == "ask"
    # Empty defaults must mean "no restriction configured", which the server treats as unset.
    assert PLUGIN["userConfig"]["connect_allow"]["default"] == ""
    assert PLUGIN["userConfig"]["upload_roots"]["default"] == ""


def test_marketplace_lists_this_plugin_from_the_repository_root():
    assert MARKET["owner"]["name"]
    assert [(p["name"], p["source"]) for p in MARKET["plugins"]] == [(PLUGIN["name"], "./")]


@pytest.mark.skipif(shutil.which("claude") is None, reason="Claude Code CLI not installed")
@pytest.mark.parametrize("target", [".", ".claude-plugin/plugin.json"])
def test_claude_plugin_validate_strict_passes(target):
    res = subprocess.run(["claude", "plugin", "validate", "--strict", target], cwd=ROOT,
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stdout + res.stderr
