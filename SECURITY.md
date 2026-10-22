# Security notes

## Debugging access

Termius must be started with a debugging port for this adapter to work. Any local
process that can reach that port can execute JavaScript in its renderer, including
accessing decrypted data held in memory. The adapter's metadata filters do not
protect against direct CDP access. Bind to `127.0.0.1`, verify the listener, use
the port only when needed, then quit and restart Termius normally. Do not expose
or forward the port to other hosts or make debugging a default login service.

The MCP server uses stdio and opens no inbound network listener of its own. Its
CDP client accepts only the configured `ws://127.0.0.1:<port>` endpoint and a
Termius primary renderer URL. It rejects a running Termius whose CDP User-Agent
reports a version other than 9.38.1, before injecting anything.
These checks are compatibility and targeting checks, not authentication of other
same-user processes.

## Tool permissions and data

- Host/keychain listings return selected metadata, not password or private-key
  fields. `list_keychain` is not registered unless `TERMIUS_ENABLE_KEYCHAIN=1`.
  Terminal output can still contain secrets, host addresses or private
  files printed by the user or a command. Review it before sharing transcripts.
- `sftp_upload` refuses credential stores on the resolved local path (`~/.ssh`,
  `~/.gnupg`, `~/.aws`, `~/.kube`, `~/.netrc`, `~/.codex`, `~/.claude`, macOS
  keychains and similar, plus any `.ssh`/`.gnupg` directory), even inside
  `TERMIUS_UPLOAD_ROOTS`. That variable optionally limits uploads to listed
  directories; a value naming no directory is refused as a configuration error.
  Neither replaces the client's own approval of each upload.
- `connect_host` can connect a saved host using its Termius configuration. By
  default, any saved label is eligible. `TERMIUS_CONNECT_ALLOW` limits new
  connections by label; it does not constrain existing terminal sessions or SFTP.
- First-time host-key prompts default to `TERMIUS_HOSTKEY=ask`. Changed-key
  dialogs are never automatically accepted. Optional `add` and `once` modes relax
  first-use confirmation and should be an explicit operator choice.
- Commands and input can execute destructive operations with the remote account's
  privileges. Tool annotations describe this risk; the MCP client must enforce
  its own approvals. Prompt checks are not a shell sandbox.
- Terminal claims and file locks coordinate cooperating MCP processes. They do
  not stop human typing, same-user tampering, or direct debug-port access. Several
  agents using one server process share that process's claims.

## Command observation

Common REPL and shell continuation prompts (`>`, `>>>`, `dquote>`, `db=#`,
`mysql>` and similar) are refused, but the prompt heuristic can still mistake
another program's prompt for a shell.
`prompt-observed` does not establish an exit code. Terminal text is rendered,
filtered and capped, rather than a complete byte-exact execution log. Buffer
loss and observation failure are reported explicitly. A timeout or lost
acknowledgement does not prove input was not sent; never blindly retry a command.

Input and claims are refused with multiple primary windows; reads can be partial
and include a warning. Version checks cannot guarantee that every private object
shape or application state is supported.

## SFTP transfers

Uploads require an explicit file, remote SFTP session and matching destination
directory. Only one regular file is supported. Native overwrite dialogs are not
accepted by the adapter. Monitor the original transfer ID after a wait timeout;
the upload may still be running. Completion requires the native end event and
matching final file counts, not progress percentage alone.

Recoverable errors retain observers so a human-initiated native Retry can update
the same transfer; earlier errors remain in `prior_errors`. Transfer observations
are held in the renderer and are not durable across application restarts or bridge
replacement. An unknown result must be investigated in Termius and at the remote
destination before trying again.

## Evidence and reporting

The published tests use simulated Termius objects; limited live checks cover one
macOS Termius 9.38.1 installation. Live error/retry/cancel, overwrite and large-file
coverage is not established. See [validation scope](docs/VALIDATION.md).

No Termius terms-of-service or proprietary-license compatibility review has been
completed. This project distributes its own adapter and upstream MIT code, not
the Termius application.

Do not attach credentials, vault exports, private terminal logs or real host
addresses to public issues. Use GitHub private vulnerability reporting if it is
enabled; otherwise request a private reporting channel without including the
sensitive details in a public issue.
