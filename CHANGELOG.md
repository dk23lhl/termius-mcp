# Changelog

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
