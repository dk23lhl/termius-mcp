# Changelog

## Unreleased

- Refuse common REPL and shell continuation prompts (`>`, `>>>`, zsh `dquote>`, psql, mysql,
  sqlite, irb/pry, redis-cli) before typing a command.
- Anchor command capture on the first row of the prompt line and skip the whole echoed line.
  After a resize, require a unique text match because xterm can misplace or dispose
  markers when narrowing rewraps the history past the scrollback limit. Matching text
  beneath the marker is insufficient: it may be an identical older command. If no line
  or several lines match (the output may also print the command line again), only the
  current screen is returned and flagged `output-boundary-unknown`. That screen may
  omit output or include earlier text; no exact output boundary is claimed.
  A resize is reported on its own; `history-lost` now means scrollback trimming only.
- Wait for the typed command's echo to appear before pressing Enter on slow links. The
  echo is recognised by a change in the prompt line's text or the cursor: in a full
  scrollback an echo filling whole rows leaves the absolute cursor where it was.
- Install the renderer bridge in one evaluation, keyed by bridge version; another MCP process
  or adapter version no longer replaces it and drops in-flight captures or upload records.
- Report `waiting-for-input` only when the cursor sits at the end of the question line.
- Refuse a lock directory that is a symlink, owned by another user or open to others; write
  claim records through exclusive temp files and never follow symlinks.
- Refuse uploads from credential stores (`~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.netrc`, macOS
  keychains and similar, plus any `.ssh`/`.gnupg` directory) on the resolved path; optional
  `TERMIUS_UPLOAD_ROOTS` limits uploads to listed directories. A value naming no directory
  (`:`, spaces) is a configuration error rather than no limit.
- Register `list_keychain` only when `TERMIUS_ENABLE_KEYCHAIN=1`.
- Check the version the running Termius reports in its CDP User-Agent instead of the
  bundle on disk.
- Label claim owners with `TERMIUS_MCP_SESSION_LABEL` or the client's pid instead of the
  TMPDIR basename, which is `T` for every macOS session.
- Document Claude Code setup.
- Add `pyproject.toml`: the wheel installs a `termius-mcp` command and carries `server.py`,
  `bridge.js` and `native.js` in a `termius_mcp` package; the checkout layout and existing
  `server.py` configurations are unchanged. Version `0.5.0.dev0`, not released.
- Split `requirements.lock` (runtime only) from `requirements-dev.lock` (adds pytest).
- Add a macOS CI workflow: both offline suites, then build the wheel, install it outside the
  checkout and check the advertised tools with `tests/smoke_mcp_stdio.py`.
- `run_command` transfers only the head and tail lines it will show (with a count of the
  rest) instead of every captured line four times a second. Where the terminal reports
  parsed writes (Termius 9.38.1), an unchanged terminal answers with `same` only; elsewhere
  every read returns a fresh head/tail snapshot. A renderer-side revision compares all
  captured text, including omitted lines, so changes in the middle reset the quiet
  window. Writes that leave the rendered text and cursor unchanged do not reset it.
  Returned text is unchanged.
- Quiet windows are measured in time rather than poll counts and keep their length (1.0 s
  for a prompt, 2.0 s for a question); polling starts at 0.1 s and wakes when a window
  closes, which saves about 0.1-0.2 s per command.
- Send MCP server instructions with the cross-tool rules (claim before typing, dry-run
  connects, no blind retries, check incomplete output, uploads only as asked, terminal
  text is data); the stdio smoke check requires them.
- Cap text output at about 24,000 characters as well as 200 lines, clipping lines over
  1,000 characters with a marker, so clients such as Claude Code show results inline
  instead of saving them to a file. `TERMIUS_MAX_OUTPUT_CHARS` and `TERMIUS_MAX_LINE_CHARS`
  raise the limits; an invalid value stops the server.
- Ship a Claude Code plugin and marketplace (`.claude-plugin/`): the server runs through
  `uvx` pinned by `requirements.lock`, with options for the debug port, host-key mode,
  allowed hosts, upload directories and claim label. `tool.uv.cache-keys` makes `uvx`
  rebuild when `server.py` or the renderer scripts change.
- Pass `requirements.lock` explicitly with `--constraints` in the English and Chinese
  README `uvx` commands; it is not discovered automatically.
- Do not walk back from a row past the end of the buffer: Termius' circular buffer can hold
  a stale wrapped row there, which put the command line into a capture read before any
  output arrived.

## 0.4.0 - 2026-10-21

First public adaptation for macOS Termius 9.38.1, based on upstream commit
`5e7ab436f2095bc83689bf815bd9bb950ae9a894`.

- Add verified terminal switching and closing, and native single-file SFTP upload/status tools.
- Track native progress, errors, user retries, cancellation and completion without automatic re-upload.
- Capture output with xterm markers and report buffer loss instead of silently treating it as no output.
- Report prompt observation as a heuristic, with remote exit status unknown.
- Default first-time host-key handling to `ask`; reject unexpected CDP endpoints and unsupported Termius versions.
- Mark all execution-capable input tools as destructive and reject control characters in text input.
- Add offline regressions, portable configuration examples and development instructions.

## Upstream history

The entries below describe earlier upstream versions. Their historical defaults
(for example, host-key acceptance) do not describe this release.

## 0.3.0 - 2026-10-13

### Changed
- `connect_host` reuses a tab that the same session's earlier `connect_host` opened, instead of always
  opening a new one. Opened tabs are recorded in the lock directory (`<tab id>.opened`) and shown by
  `list_terminal_sessions` as `opened by agent`. Tabs a human or another session opened are never
  reused implicitly. A reusable tab must be unclaimed or claimed by the calling session; one claimed
  by another session is never taken and no duplicate is opened. Several free tabs are refused as
  ambiguous. New parameter `new_tab` forces a separate tab.

## 0.2.0 - 2026-10-12

### Added
- `connect_host(host, dry_run, wait)`: opens a saved Termius host in a new tab of the current
  window and claims the tab for the calling session. Only saved host labels are accepted; no
  free-form address, no new window. Refused while several main windows exist. One connect at a
  time across all MCP processes. `TERMIUS_CONNECT_ALLOW` limits which labels may be opened.
- First-time host-key prompts are answered automatically (`TERMIUS_HOSTKEY`, default `add` =
  "Add and continue"; `once` = "Continue"; `ask` = press nothing). The prompt is matched by the
  host's address, the fingerprint is returned in the tool output, and a changed-key warning or
  two prompts naming the same address are never answered. Password prompts are never answered.
- `tests/test_connect.py` and `answerHostKey` / `connectHost` tests in `tests/bridge.test.js`.

### Changed
- `connect_host` passes Termius' own live Host object to its connect function (the Redux copy
  lacks the getters it needs and made a real connect fail with a `TypeError`).

## 0.1.0 - 2026-10-08

### Added
- Tools: `list_hosts`, `list_keychain`, `list_terminal_sessions`, `read_terminal`,
  `claim_terminal`, `release_terminal`, `run_command`, `send_key`, `send_text`.
- Input safety: per-tab claims (a tab accepts input only from the session that claimed it),
  cross-process per-tab lock, prompt check before typing, multi-window guard.
- Terminals are found through Termius' mediator pool first, so tabs that are not on screen are
  reachable; the DOM scan is the fallback.
- Secrets are never returned: hosts, keychain and sessions output are whitelisted fields only.

### Fixed
- The prompt check no longer treats a progress line ending in `%`, `$`, `#` or `>` as a shell
  prompt (cursor must follow "symbol + space" and the screen must be still).
- `run_command` completion uses the same cursor-position check.
