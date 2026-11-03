# Skills

Three Claude skills teach Claude to use the knowledge base in conversation. They only instruct; they hold no data and call the [MCP server](mcp.md) (`search_facts`, `list_subjects`, `add_fact`), so they work wherever that server is connected.

| Skill (`skills/<name>/`) | What it does |
| --- | --- |
| `knowledge-recall` | Search before answering anything the base may already know; cite trust level and recheck date; "not recorded" is not "false". |
| `knowledge-record` | Search, then `add_fact` once per claim with a source quote, honest trust and freshness. Only when the user asks or agrees. |
| `knowledge-freshness` | Compare each fact's `recheck_by` with today and offer to re-verify what is past it. |

Search and list rows carry `recheck_by` for this. Visibility and mode stay the server's decision (`mcp_mode` in the config file, default `normal`: private facts are not returned): the skills never filter private facts themselves.

## Install

Local only: the MCP server is a stdio child of your own client. There is deliberately no remote connector, so web claude.ai and the mobile apps cannot reach it.

**Claude Code.** Register the server (see [mcp.md](mcp.md)), then copy or link the skill folders into `~/.claude/skills/`:

```bash
cp -R skills/knowledge-* ~/.claude/skills/
```

If you manage `~/.claude/skills` with a dotfiles tool, put the folders in its source tree instead.

**Claude Desktop (chat).** Add the same server to `claude_desktop_config.json` (Settings > Developer > Edit Config), with absolute paths, and restart:

```json
{
  "mcpServers": {
    "knowledge": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/knowledge", "python", "/path/to/knowledge/mcp_server.py"]
    }
  }
}
```

Then zip the skills and upload each in Settings > Capabilities > Skills ("Upload a skill"; one ZIP per skill folder):

```bash
cd skills && for d in knowledge-*; do zip -r "$d.zip" "$d"; done
```

Skills uploaded there belong to your account. Whether they work in the mobile apps is not documented, and the server does not work there either.

## Why no remote server

The server can read private facts and write to the private repo. Anything remote would have to be read-only, OAuth-protected, and built from the normal-only database (`knowledge-normal.db`, which cannot contain private facts). That is not built; it needs a separate decision.
