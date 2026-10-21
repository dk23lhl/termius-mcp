# Validation scope

## Offline coverage

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
