"""The MCP server as a use case: one JSON-RPC message in, at most one reply out.

Application ring (tach layer `application`): it orchestrates through the ports in `mcp_port` and the rules in `mcp_rules`, and imports no adapter.

`handle_line(deps, line)` takes the text of a single request and returns the text of the reply (None for a
notification). It reads facts and records them only through the ports in `mcp_port`, holds no state, and has no
input or output of its own: `mcp_server` reads the lines and writes the replies. The rules (tool catalog,
argument checks, reply wording) are in `mcp_rules`.
"""

__arbor__ = {"ring": "application"}

from dataclasses import dataclass

import mcp_rules as rules
from fact_reads_port import DatabaseNotFound, FactQueryError
from mcp_port import FactCapture, FactLookup, Session


@dataclass(frozen=True)
class McpDeps:
    """Everything this service reaches the world through; the composition root supplies each one."""
    capture: FactCapture
    lookup: FactLookup
    session: Session


def handle_line(deps, line):
    """The reply to one message as a single line of JSON, or None when no reply is due. Never raises."""
    message, bad = rules.decode(line)
    if bad is not None:
        return rules.encode(bad)
    is_notification = "id" not in message
    try:
        reply = _dispatch(deps, message)
    except Exception as e:   # a request must always be answered: one bad call must not end the session
        reply = rules.error_message(message.get("id"), rules.INTERNAL_ERROR, f"{type(e).__name__}: {e}")
    return None if is_notification else rules.encode(reply)


def _dispatch(deps, message):
    method, params, id_ = message["method"], message.get("params"), message.get("id")
    if method == "initialize":
        return rules.result_message(id_, rules.initialize_result(params))
    if method == "ping":
        return rules.result_message(id_, {})
    if method == "tools/list":
        return rules.result_message(id_, {"tools": rules.tool_list()})
    if method == "tools/call":
        return _call(deps, id_, params)
    return rules.error_message(id_, rules.METHOD_NOT_FOUND, f"method not found: {method}")


def _call(deps, id_, params):
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        return rules.error_message(id_, rules.INVALID_PARAMS, "tools/call needs a tool name")
    name = params["name"]
    if name not in rules.TOOL_NAMES:
        return rules.error_message(id_, rules.INVALID_PARAMS, f"unknown tool: {name}")
    arguments = params.get("arguments", {})
    try:
        return rules.result_message(id_, _run_tool(deps, name, arguments))
    except DatabaseNotFound as e:
        return rules.result_message(id_, rules.tool_result(f"{e}. Run `knowledge.py build` first.", True))
    except FactQueryError as e:
        return rules.result_message(id_, rules.tool_result(f"search failed: {e}", True))
    except Exception as e:   # a failing port is a tool error the caller can read, not a protocol failure
        return rules.result_message(id_, rules.tool_result(f"{name} failed: {type(e).__name__}: {e}", True))


def _run_tool(deps, name, arguments):
    if name == "add_fact":
        fact, problems = rules.parse_add_fact(arguments, deps.session.session_id)
        return rules.problems_result(problems) if problems else rules.add_fact_result(deps.capture.record_fact(fact))
    if name == "search_facts":
        parsed, problems = rules.parse_search(arguments)
        if problems:
            return rules.problems_result(problems)
        terms, filters = parsed
        return rules.rows_result(deps.lookup.search_facts(terms, **filters))
    include_pending, problems = rules.parse_subjects(arguments)
    return rules.problems_result(problems) if problems else rules.rows_result(deps.lookup.list_subjects(include_pending))
