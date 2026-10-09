"""CLI-first parity registry (issue #34).

Rule: every action reachable from a UI button, the inbox or an MCP tool is also
a CLI command. Order of work for any new action: library function first (pure,
importable, returns data), Typer command in knowledge.py second (thin printer,
`--json` for reads), MCP tool / inbox handler third.

`ACTIONS` maps action name -> CLI command path (a tuple of words after
`knowledge.py`). Add the entry in the same change that adds the command.
Future layers declare what they expose in a module-level `PARITY_ACTIONS`
(an iterable of action names): `mcp_server.PARITY_ACTIONS` for MCP tool names,
`inbox.PARITY_ACTIONS` for inbox action names. tests/test_cli_parity.py imports
those modules when they exist and fails on any name missing from `ACTIONS`.

Pure data and pure functions: no typer import here, so the registry can be
read by any layer; the test passes in the click command tree.
"""

ACTIONS = {
    "build": ("build",),
    "add_fact": ("add-fact",),
    "add_facts": ("add-facts",),
    "clean_concerts": ("clean-concerts",),
    "search_facts": ("search",),
    "get_fact": ("show",),
    "list_subjects": ("subjects",),
    "list_facts": ("facts",),
    "get_history": ("history",),
    "list_pending": ("review-pending",),
    "approve": ("approve",),
    "reject": ("reject",),
    "audit_claims": ("audit-claims",),
    "audit_sources": ("audit-sources",),
    "build_info": ("build-info",),
    "source_ids": ("source", "ids"),
    "audit_source_status": ("audit-source-status",),
    "entity_list": ("entity", "list"),
    "entity_show": ("entity", "show"),
    "entity_add": ("entity", "add"),
    "entity_alias": ("entity", "alias"),
    "entity_tag": ("entity", "tag"),
    "entity_untag": ("entity", "untag"),
    "entity_migrate_keywords": ("entity", "migrate-keywords"),
    "subject_list": ("subject", "list"),
    "subject_show": ("subject", "show"),
    "subject_alias": ("subject", "alias"),
    "subject_describe": ("subject", "describe"),
    "subject_deprecate": ("subject", "deprecate"),
    "migrate_memory": ("migrate-memory",),
    "supersede_fact": ("supersede",),
    "retract_fact": ("retract",),
    "set_visibility": ("set-visibility",),
    "privacy_check": ("privacy", "check"),
    "privacy_rules": ("privacy", "rules"),
    "privacy_tag": ("privacy", "tag"),
    "privacy_untag": ("privacy", "untag"),
    "privacy_add_keyword": ("privacy", "add-keyword"),
    "privacy_remove_keyword": ("privacy", "remove-keyword"),
    "serve_inbox": ("inbox",),
    "edit_fact": ("edit",),
    "make_private": ("set-visibility",),   # inbox: raise-only use of set-visibility
    "approve_shown": ("approve",),          # inbox: approve exactly the facts shown
}

# Optional future layers that must declare PARITY_ACTIONS.
LAYER_MODULES = ("mcp_server", "inbox")


def resolve_command(group, path):
    """Walk a click Group by command words; the command, or None if any word is missing."""
    cmd = group
    for word in path:
        commands = getattr(cmd, "commands", None)
        if commands is None or word not in commands:
            return None
        cmd = commands[word]
    return cmd if path else None


def actions_without_command(actions, group):
    """Registered action names whose CLI path does not resolve to a real command."""
    return sorted(name for name, path in actions.items() if resolve_command(group, tuple(path)) is None)


def declared_without_entry(declared, actions):
    """Names a layer declares that have no registry entry (ACTIONS)."""
    return sorted(set(declared) - set(actions))
