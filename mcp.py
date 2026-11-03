"""Composition root for the MCP use case: binds `mcp_service` to the single-fact use case (`add_fact`) and the
read side (`queries`), and publishes `handle_line`. `session_id` is supplied by the entry point (`mcp_server`),
so nothing here generates an identifier or reads the environment."""
from dataclasses import dataclass

import mcp_service
from wiring import bind_all


@dataclass(frozen=True)
class RunSession:
    """The `Session` port: the identifier this server run stamps on the facts it records."""
    session_id: str


def compose(add_fact_api, queries_api, session_id):
    """`handle_line(line)` wired to these use cases; each is checked against its port at startup."""
    deps = mcp_service.McpDeps(capture=add_fact_api, lookup=queries_api, session=RunSession(session_id))
    return bind_all(mcp_service, deps, ("handle_line",))
