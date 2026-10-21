# Termius MCP

[中文文档](README.zh-CN.md) · [Quick start](#install) · [Security](SECURITY.md) · [Validation](docs/VALIDATION.md)

**Your Termius sessions, ready for AI.**

Read terminal output, run commands, switch sessions, and follow SFTP uploads from
an MCP client such as Codex — inside the Termius app you already use.

Keep your saved hosts and native transfer queue. Give your agent explicit session
IDs, terminal text, and transfer status for the work that otherwise takes repeated
screenshots and clicks.

This community adaptation of
[deltaguita/termius-mcp](https://github.com/deltaguita/termius-mcp) targets
**macOS Termius 9.38.1**. It is not an official Termius integration.

The adapter uses Termius's existing sessions and native transfer queue through
its local debugging interface. It does not create a separate SSH/SFTP client.
Other Termius versions are rejected before CDP access; Windows and Linux desktop
support have not been implemented.

## Why pair it with Computer Use?

Computer Use is useful for navigating Termius and handling dialogs. Terminal work
also needs precise text and state: logs scroll, focus moves between tabs, and a
full progress bar does not prove that an upload finished. Termius MCP gives those
repeated operations dedicated tools.

| Friction in a screenshot-and-click workflow | What this adapter provides |
| --- | --- |
| Reading long terminal output across screenshots | Text from the xterm buffer, command-output markers, and an explicit warning when history is lost. Output is capped at 200 lines. |
| Keeping track of the intended terminal as focus changes | Explicit session IDs for reads and input; tools to activate and close the selected session. |
| Deciding what a quiet screen means | Reported states such as timeout, waiting for input and prompt observed. Prompt observation remains a heuristic; remote exit code is unknown. |
| Watching a small SFTP progress bar and checking for errors | A transfer ID with native progress, error and completion observations. A wait timeout keeps the original upload available for follow-up. |
| Multiple agents typing into the same terminal | Claims and cross-process locks for cooperating MCP callers. Human typing remains outside these locks. |

Use Computer Use for setup, SFTP pane selection and dialogs; use MCP for the
repeated terminal and transfer operations. The app stays visible so you can inspect
what is happening.

### Ask for the task

Once configured, you can give your agent requests such as:

> Show me the open Termius sessions and read the output from the one I select.
>
> Connect to my saved development host in a new terminal, then run `pwd` there.
>
> Upload this file to the prepared SFTP destination. Follow the same transfer until
> it completes or reports an error, and tell me the result.

The agent operates through named tools while you keep working in a familiar SSH
client. It does not need you to copy terminal output back into the chat.

## Capabilities

| Operation | Tools |
| --- | --- |
| Inspect saved hosts and open terminals | `list_hosts`, `list_terminal_sessions`, `read_terminal` |
| Connect a saved terminal host | `connect_host` |
| Coordinate terminal input | `claim_terminal`, `release_terminal` |
| Execute commands or send input | `run_command`, `send_text`, `send_key` |
| Select or close a terminal | `switch_terminal`, `close_terminal` |
| Upload one regular file and inspect its native transfer | `list_sftp_sessions`, `sftp_upload`, `sftp_transfer_status` |

The server also advertises `list_keychain`, which returns selected identity
metadata. The supplied Codex configuration intentionally excludes it and enables
the 14 tools above. Metadata filtering does not redact secrets printed by a
remote command in the terminal.

## Requirements

- macOS, with **Termius 9.38.1** installed at `/Applications/Termius.app`.
- Python 3.11 or newer; Python 3.12 was used for validation.
- Saved Termius hosts and access already configured by you.
- Node.js 20 or newer only for the JavaScript development tests; Node.js 24 was
  used for validation. Node.js is not required to run the MCP server.

The version check is intentional: the integration uses private React, xterm and
transfer-queue interfaces. Removing the check does not establish compatibility.

## Install

Clone this repository and create a Python environment:

```sh
git clone https://github.com/dk23lhl/termius-mcp.git
cd termius-mcp
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
```

The lock file pins the runtime and offline Python test dependencies. It does not
contain credentials or machine-specific package paths.

### Enable Termius access when needed

Save your work and **quit Termius first**. Restarting it disconnects existing
terminal sessions. Then start it with a loopback debugging port:

```sh
open -a /Applications/Termius.app --args --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222
lsof -nP -iTCP:9222 -sTCP:LISTEN
```

Continue only if the listener is `127.0.0.1:9222`, not `*` or `0.0.0.0`.
Passing these arguments to an already running Termius process does not reliably
enable debugging.

**Other processes on your Mac can access the debug interface**, including data
held in the renderer. Loopback binding does not provide authentication. Open the
port only while needed. To close it, quit Termius and reopen normally:

```sh
open -a /Applications/Termius.app
```

After a normal restart, the `lsof` command above should show no listener. The MCP
server itself never opens the port or restarts Termius. Read [SECURITY.md](SECURITY.md)
before enabling access.

### Configure Codex

Merge [codex-mcp.toml.example](codex-mcp.toml.example) into `~/.codex/config.toml`.
Replace both `/absolute/path/to/termius-mcp` placeholders with the absolute path
to your checkout. Preserve unrelated configuration and do not enable this adapter
and the upstream server together; they share a renderer bridge and lock directory.

Restart or reload your client if it has not loaded the new server. In Codex,
`/mcp` displays connected servers. An MCP configuration does not create a
same-named `@` app mention or slash-command skill. You can ask:

> Use the termius MCP to list saved hosts and open terminal sessions.

Connecting or typing into a host should follow an explicit user request. Start
with `connect_host` in dry-run mode, select the returned session ID, and claim the
terminal before input. Host-key confirmation defaults to `ask`; unexpected
changed-key dialogs are never accepted automatically.

## SFTP workflow

1. Open one SFTP view in Termius. Connect one pane to Local and the other to the
   desired remote host, then navigate the remote pane to the destination directory.
2. Call `list_sftp_sessions` and select the exact remote session ID.
3. Call `sftp_upload` with the absolute local file path, that session ID, and the
   exact remote directory. Directories and symbolic links are refused.
4. If waiting times out, query the returned `transfer_id` with
   `sftp_transfer_status`. **Do not upload the file again just because waiting
   timed out.**

The tool observes native queue progress and error/end events; it does not read
pixels from the progress bar. `100%` progress or a disappearing queue row alone
does not count as completion. Native overwrite dialogs remain for you to handle.
Automatic host connection currently applies to terminal sessions, not SFTP panes.

## Interpret results carefully

- `prompt-observed` is a prompt heuristic, not proof of a zero remote exit code.
  Commands run as supplied, without an added shell wrapper.
- Terminal output is rendered buffer text, not a byte-exact process log. Long
  output is capped at 200 lines; buffer trimming or resizing is reported as
  `history-lost`. `run_command` preserves interior blank lines.
- A timeout does not cancel a command or upload. Lost acknowledgements may mean
  input was delivered; inspect the existing session or transfer before retrying.
- Claims coordinate cooperating MCP processes. They do not block human typing or
  prevent a local process from connecting directly to the debug port.
- `TERMIUS_CONNECT_ALLOW` restricts `connect_host` only, not access to existing
  sessions. It is not a complete host-access policy.

## Development and validation

Offline tests do not require Termius, a debug port, credentials, or a remote host:

```sh
.venv/bin/python -m pytest tests -q
npm ci --ignore-scripts
npm test
```

JavaScript tests use the real `@xterm/headless@5.5.0` buffer/parser; Termius
React/Redux objects and transfer streams are test doubles. See
[validation scope](docs/VALIDATION.md) for live checks and untested cases.

## Origin and license

Based on upstream commit
[`5e7ab436f2095bc83689bf815bd9bb950ae9a894`](https://github.com/deltaguita/termius-mcp/tree/5e7ab436f2095bc83689bf815bd9bb950ae9a894).
The upstream copyright notice and [MIT license](LICENSE) are preserved.
[CHANGELOG.md](CHANGELOG.md) distinguishes this adaptation from upstream history.
This repository contains adapter code and tests, not the Termius application or
its bundled source code. Termius is a separate product and is not affiliated with
this project.
