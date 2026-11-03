# MCP server

`mcp_server.py` lets Claude research a topic and add what it finds to the knowledge repository.

Register it as a local stdio server (use the absolute path of your checkout):

```bash
claude mcp add knowledge -- uv run --project /path/to/knowledge python /path/to/knowledge/mcp_server.py
```

## Tools

| Tool | CLI twin | What it does |
| --- | --- | --- |
| `search_facts` | `search` | Full-text search of the built `knowledge.db`; check it before adding. |
| `list_subjects` | `subjects` | Existing subjects, to file a new fact under one. |
| `add_fact` | `add-fact` | Record one fact with its source quote, trust level and freshness. |

## Mode

The server runs under the `mcp_mode` setting in the config file (or `KNOWLEDGE_MCP_MODE`), fixed for the life of the process:

| `mcp_mode` | Reads | Writes |
| --- | --- | --- |
| `normal` (default) | Only `normal` facts on non-private subjects, active only; private subjects are not listed. | `add_fact` works (new facts are stored private and pending). |
| `private` | Everything. | `add_fact` works. |
| `off` | Every tool call is refused. | Refused. |

Set `mcp_mode = "private"` if you want the assistant to see your private facts. Restart the client after changing it.

## What the server decides, not Claude

- A fact added here is always `captured_via: mcp`, `pending` (a person approves it with `knowledge.py approve` or the inbox) and `private` unless asked otherwise (a privacy rule may still raise it).
- `source_quote` is required; `session_id` defaults to an id generated per server run.
- Freshness is required: `recheck_by`, or `no_decay` with a `recheck_rationale`.
- Arguments the schema does not list are refused, not ignored. A dirty private repo refuses the write; a fact written but not committed comes back as an error naming the file.
- Search reads the last built database: a new fact shows up after `knowledge.py build`.

## Layers

| Module | Layer | I/O |
| --- | --- | --- |
| `mcp_server` | serving | the only one: reads lines from stdin, writes replies to stdout |
| `mcp` | composition | none; binds the use case to `add_fact` and `queries` |
| `mcp_service` | application | none; one JSON-RPC line in, one reply out, through ports |
| `mcp_rules`, `mcp_port` | domain services | none; tool catalog, argument checks, reply wording, ports |

`tests/test_mcp_service.py` fails if a core module imports an I/O module or calls `print`/`open`.

The server runs as the user and can read private facts: keep it a stdio child of your own client, never a network service.
