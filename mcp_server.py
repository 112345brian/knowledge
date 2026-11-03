"""MCP server over stdio (#2): lets Claude search the knowledge repository and add researched facts to it.

    python3 mcp_server.py            (register it with Claude as a stdio server; see docs/mcp.md)

This is the only module of the feature that reads or writes: it reads one line from the input stream, hands it
to `mcp.handle_line` (the use case, which is pure text in, text out) and writes the reply. Diagnostics go to the
error stream, never the output stream, which belongs to the protocol. Newline-delimited JSON-RPC, as the MCP
stdio transport specifies; standard library only.

LOCAL ONLY. The server can read private facts and write to the private repo as the user, so it is a stdio child
of the user's own client, never a network service. Every tool has a CLI twin (PARITY_ACTIONS).
"""

__arbor__ = {"ring": "serving"}

import sys
import uuid

import container
import mcp
from mcp_rules import PARITY_ACTIONS  # noqa: F401  (the actions this layer exposes; tests/test_cli_parity.py)


def serve(app, reader, writer, session_id, errors):
    """Answer every line of `reader` on `writer` until it ends. A line that cannot be read or answered is
    reported on `errors` and skipped: the loop never ends because of one message."""
    handler = mcp.compose(app.mcp_capture, app.mcp_lookup, session_id).handle_line
    for line in reader:
        try:
            reply = handler(line)
            if reply is not None:
                writer.write(reply + "\n")
                writer.flush()
        except Exception as e:    # the use case never raises; this is the stream failing (closed pipe, bad bytes)
            print(f"mcp_server: {type(e).__name__}: {e}", file=errors, flush=True)
            if isinstance(e, BrokenPipeError):
                return


def main():
    serve(container.Container(), sys.stdin, sys.stdout, f"mcp-{uuid.uuid4().hex[:12]}", sys.stderr)


if __name__ == "__main__":
    main()
