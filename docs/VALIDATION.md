# Validation scope

## Review follow-up, 2026-10-22 (offline only)

The full suites passed in the existing local test environment: Python 3.12,
257 passed with the existing Pydantic `IncompleteFieldDefinitionWarning`; Node.js 24,
61 passed. This includes MCP stdio discovery and strict validation of the Claude Code
manifests. `git diff --check` passed, and the installed `uvx --help` confirms the
documented `--constraints` option.

Two remaining capture defects were reproduced before the fixes:

- A changing omitted middle, with unchanged transferred head/tail text and cursor,
  could satisfy the prompt or input quiet window. The renderer now compares the full
  normalized captured text and sends an output revision. Regressions cover both
  completion heuristics, real xterm middle-line rewrites with and without parsed-write
  events, and writes that change no rendered text. Such empty writes preserve the
  existing quiet-window behavior.
- After reflow, a marker could land on identical older command text. Real xterm tests
  now require an ambiguous-boundary report whenever the anchor text appears more than
  once, including when the marker still points at matching text. The result is the
  current screen with `output-boundary-unknown`; it may contain earlier text and is
  not represented as exact command output.

The head/tail transfer limit and the 1.0 s / 2.0 s quiet windows are unchanged. The
full text snapshot stays in the renderer for comparison; it is not sent through CDP.
The README `uvx` examples now explicitly pass the runtime lock as constraints.

These follow-up checks do not contact Termius or a remote host. They do not establish
live latency or memory usage, and do not repeat the package installation or live
checks below. No global client configuration was changed.

## Earlier unreleased validation, reported 2026-10-22

The following records describe the implementation at commit `cc1d159`, before the
review follow-up above. In particular, its resize path still trusted a marker when
the text beneath it matched; current code requires a unique text match instead.

Offline, from this directory: Python 3.12 suite 254 passed (the same one dependency
warning), in a fresh environment from `requirements-dev.lock`; Node.js suite 58
passed; MCP stdio discovery advertises 14 tools by default (`list_keychain` only with
`TERMIUS_ENABLE_KEYCHAIN=1`); the Codex example TOML parses. The CI workflow's three
run steps were executed locally (not on GitHub): the wheel contains `server.py`,
`bridge.js` and `native.js` under `termius_mcp/`, and once installed in a separate
environment its `termius-mcp` command, started outside the checkout, advertised the
14 default tools; so did `uvx --from <checkout> termius-mcp`.
Capture tests use the real `@xterm/headless@5.5.0` buffer, including resizes that
rewrap the history past the scrollback limit, output that repeats the command line,
and an echo that leaves the absolute cursor unchanged.

Live, on macOS Termius 9.38.1 with one authorized saved SSH host (Linux, bash), in a
terminal tab the maintainer chose, using harmless commands only:

- The bridge installed under its own version key next to a 0.4.0 bridge that another
  running MCP client had injected; that client's `window.__kc` stayed unchanged. A
  same-version and a different-version install during a running command did not
  interrupt its capture.
- The remote echo appeared about 0.6 s after typing. Enter was sent only after the
  cursor moved, and a three-row wrapped echo was kept out of the output.
- Narrowing the terminal during a command, restored before or after it finished,
  returned exactly the command's output plus a resize notice. The first attempt
  returned older history: the tab held about 20,000 rows under a 40,000-row
  scrollback limit, narrowing to 70 columns rewrapped them past that limit, and
  xterm trimmed rows and lost the marker. Text-based anchor recovery was added and
  the check repeated.
- With output that printed the command line again (the same text as prompt plus
  command), the full output came back with no resize, after a small narrowing, and
  after narrowing to 70 columns (the buffer reached its 40,049-row limit) with and
  without restoring during the run. In these runs the marker's line still matched, so
  the path that flags several matching lines as `output-boundary-unknown` was
  exercised offline only.
- Reduced capture transfer, against the previous commit in the same tab: `echo`,
  `seq 1 5000`, `sleep 3` and a `\r` progress loop gave identical output text. For
  `seq 1 5000` the capture reads carried about 10 KiB instead of 261 KiB; while a
  command was quiet, the renderer answered `same`, confirming that Termius 9.38.1
  exposes the parsed-write event. Over two alternating runs the new version was not
  slower (for example `sleep 3` 4.84 s against 4.91 s); run-to-run variation on this
  link was larger than the saving.
- A trace of capture reads showed the command line returned before the newline had
  arrived; the cause was a stale wrapped row just past the end of Termius' circular
  buffer. After the fix the same trace returned no lines at that point.
- Claude Code plugin: `claude plugin validate --strict` (Claude Code 2.1.285) passed for
  the marketplace and the plugin manifest. The plugin's server command, run outside the
  checkout, resolved `mcp`, `websockets`, `pydantic`, `pydantic-settings` and `starlette`
  to the versions in `requirements.lock` and sent its instructions. A one-off headless
  session started with `--plugin-dir` and default options listed the 14
  `mcp__plugin_termius-mcp_termius__*` tools, quoted the server instructions, and
  returned the real terminal list from `list_terminal_sessions`. No marketplace was
  added and no plugin installed; the user's Claude Code settings were not changed.
  Installing from GitHub (`claude plugin marketplace add`) needs a pushed repository
  and was not tried.
- `run_command` refused at a Python `>>>` prompt and at a bash `> ` continuation
  prompt; nothing was typed.
- A read-only call confirmed the running app's CDP User-Agent reports `Termius/9.38.1`
  and passes the new version check.

Checked offline only: the upload source policy (including a value naming no
directory), `list_keychain` registration, owner labels, lock-directory checks, and
echo detection when the absolute cursor does not move. Not covered live: slower
links, zsh line editing without wrap flags, transient prompts, and SFTP transfers.

## Offline coverage at the 0.4.0 release

Release preparation on 2026-10-21 ran the Python and JavaScript suites from this
publication directory. Commands are documented in the README.

| Check | Result |
| --- | --- |
| Python suite, Python 3.12 | 132 passed; one dependency warning |
| JavaScript suite, Node.js 24 | 42 passed |
| JavaScript dependency installation | `npm ci --ignore-scripts` succeeded |
| MCP stdio initialize and tool discovery | Server name `termius`; 15 tools advertised; all 14 example-allowed tools present |
| Configuration and documentation | TOML parsed; relative Markdown links resolved; upstream license preserved |

The publication checks did not contact Termius or a remote host. Runtime source
changes from the live-tested adaptation are limited to the MCP display name and
documentation/error wording; the bridge and native adapter are unchanged.

Python fixtures replace the Termius/CDP bridge. JavaScript tests simulate Termius
React/Redux state and native transfer streams; four buffer-capture tests use the
real `@xterm/headless@5.5.0` parser and bounded scrollback. These tests do not prove
live compatibility with arbitrary Termius versions or remote environments.

The pinned Python dependencies currently emit one Pydantic
`IncompleteFieldDefinitionWarning` about the FastMCP `lifespan` field. It was
observed during tests and MCP initialization, rather than suppressed.

## Limited live checks before publication

The adaptation was checked on macOS with Termius 9.38.1 on 2026-10-21, using one
authorized saved host and a dedicated terminal session. Checks covered:

- A loopback-only debug listener and real host, terminal and SFTP listings.
- Saved-host connection, claiming/releasing, reading, switching and closing.
- Command output containing Chinese text and interior blank lines.
- A timed-out running command and refusal to send a second command into it.
- One 119-byte text upload through the native SFTP queue. After an initial wait
  timeout, the same transfer ID reached `completed`; remote text and byte count
  matched the source file.
- Codex configuration discovery of the 14 allowed tools in a fresh Codex process.

The first native upload attempt revealed that the 9.38.1 transfer queue is a tuple
array rather than a Map. That attempt was refused before starting; the adapter and
regression fixture were corrected before the successful upload.

Host identifiers, local paths, terminal transcripts and raw live-test artifacts
are deliberately excluded from this public repository. This document is a
maintainer-reported summary, not a reproducible public record of those private
live sessions. Progress observations came from native events, not visual
verification of progress-bar pixels.

## Not established by the live checks

- Actual network failure, cancellation, native Retry or overwrite-conflict flows.
- Large files, long transfers, multiple application windows or other hosts.
- Other Termius builds, Windows or Linux desktop support.
- Remote shell exit codes, production readiness or a complete security audit.

Use disposable, explicitly authorized sessions and files for additional live
validation. Offline tests alone are not permission to restart Termius, expose its
debug port, connect to a remote system or upload a file.
