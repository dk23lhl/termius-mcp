"""Start an MCP server command over stdio and check its name and default tool set.

    python tests/smoke_mcp_stdio.py termius-mcp
    python tests/smoke_mcp_stdio.py .venv/bin/python server.py

CI runs it against the installed package from outside the checkout. Tool discovery
does not contact Termius, so no debug port is needed. Exits non-zero on a mismatch."""
import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

DEFAULT_TOOLS = {
    "list_hosts", "list_terminal_sessions", "read_terminal", "connect_host",
    "claim_terminal", "release_terminal", "run_command", "send_text", "send_key",
    "switch_terminal", "close_terminal", "list_sftp_sessions", "sftp_upload",
    "sftp_transfer_status",
}


async def discover(command: list[str]) -> tuple[str, set[str], str]:
    params = StdioServerParameters(command=command[0], args=command[1:])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            tools = await session.list_tools()
    return info.serverInfo.name, {t.name for t in tools.tools}, info.instructions or ""


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    name, tools, instructions = asyncio.run(discover(argv))
    ok = name == "termius" and tools == DEFAULT_TOOLS and "claim_terminal" in instructions
    print(f"server={name} tools={len(tools)} instructions={len(instructions)} chars "
          f"default_set={'ok' if ok else 'MISMATCH'}")
    if not ok:
        print("unexpected:", sorted(tools ^ DEFAULT_TOOLS), file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
