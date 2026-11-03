"""MCP tool catalog and message rules (domain: pure, no input or output).

What the tools are called, what arguments they accept, how arguments become a `NewFact` or a search, how the
outcome of a write becomes a reply, and the JSON-RPC envelopes. Everything is data in, data out: `mcp_service`
orchestrates it through the ports and `mcp_server` is the only code that touches stdin and stdout.

The server owns provenance and review state: `captured_via` is always "mcp", the fact always starts `pending`
(a person approves it in the inbox or with `knowledge.py approve`), and visibility defaults to private. A caller
cannot set any of those, and an argument the schema does not list is refused rather than ignored.
"""

__arbor__ = {"ring": "domain_services"}

import json

from fact_rules import KIND_VALUES, VALID_TRUST, VALID_VISIBILITY
from new_fact import NewFact, check_fact

SERVER_NAME = "knowledge"
SERVER_VERSION = "0.2.1"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
MAX_TEXT = 5000

# tool name -> CLI command path (tests/test_cli_parity.py checks each against cli_parity.ACTIONS)
PARITY_ACTIONS = {
    "search_facts": ("search",),
    "list_subjects": ("subjects",),
    "add_fact": ("add-fact",),
}

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603

INSTRUCTIONS = (
    "Personal knowledge repository. To record research: first call search_facts to see what is already known, "
    "then add_fact once per claim. Every fact needs the words that justified it (source_quote), an honest "
    "trust_level, and freshness (recheck_by, or no_decay with a recheck_rationale). New facts are stored "
    "private and pending: a person reviews them before they count. Search sees only the last built database, so "
    "a fact added now appears there after the next rebuild. Never put an unverified claim at trust 'verified'."
)


def _prop(kind, description, **extra):
    return {"type": kind, "description": description, **extra}


_ADD_FACT_PROPS = {
    "statement": _prop("string", "The fact, as one self-contained sentence.", maxLength=MAX_TEXT),
    "subject": _prop("string", "Lowercase kebab-case subject, e.g. 'car-maintenance'. Created if new."),
    "trust_level": _prop("string", "How far to trust it.", enum=sorted(VALID_TRUST)),
    "source_quote": _prop("string", "The words from the source (or the user) that justify the fact.", maxLength=MAX_TEXT),
    "recheck_by": _prop("string", "A date (YYYY-MM-DD) or short phrase for when to re-verify. Give this OR no_decay."),
    "no_decay": _prop("boolean", "True if the fact never goes stale; requires recheck_rationale, excludes recheck_by."),
    "recheck_rationale": _prop("string", "Why that recheck date, or why the fact does not decay.", maxLength=MAX_TEXT),
    "session_id": _prop("string", "Conversation identifier; the server supplies one when omitted.", maxLength=200),
    "kind": _prop("string", "What sort of assertion this is.", enum=list(KIND_VALUES)),
    "domain": _prop("string", "Only used when the subject is new."),
    "visibility": _prop("string", "private (default) or normal. A privacy rule may still raise it to private.",
                        enum=sorted(VALID_VISIBILITY)),
    "valid_from": _prop("string", "When it became true: YYYY, YYYY-MM or YYYY-MM-DD."),
    "valid_to": _prop("string", "When it stopped being true (same formats)."),
    "applies_to": _prop("string", "Who or what it applies to, only if the source says.", maxLength=MAX_TEXT),
    "trust_rationale": _prop("string", "Why this trust level.", maxLength=MAX_TEXT),
    "notes": _prop("string", "Free notes.", maxLength=MAX_TEXT),
    "source_citekey": _prop("string", "A citekey that already exists in the sources table."),
    "source_locator": _prop("string", "Page, section or URL fragment inside the source."),
    "is_original_claim": _prop("boolean", "True if this is a conclusion of the user's, not something a source states."),
    "is_personal": _prop("boolean", "False if the fact is not about the user personally (default true)."),
}
_ADD_FACT_REQUIRED = ("statement", "subject", "trust_level", "source_quote")
_STRING_FIELDS = tuple(k for k, v in _ADD_FACT_PROPS.items() if v["type"] == "string")
_BOOL_FIELDS = tuple(k for k, v in _ADD_FACT_PROPS.items() if v["type"] == "boolean")

_SEARCH_PROPS = {
    "terms": _prop("string", "Full-text search terms.", maxLength=MAX_TEXT),
    "subject": _prop("string", "Only facts under this subject."),
    "limit": _prop("integer", f"Most rows to return (default {DEFAULT_LIMIT}, at most {MAX_LIMIT}).",
                   minimum=1, maximum=MAX_LIMIT),
    "include_pending": _prop("boolean", "Also show facts awaiting review (default: active facts only)."),
}
_SUBJECTS_PROPS = {"include_pending": _prop("boolean", "Count facts awaiting review too.")}

_TOOLS = (
    ("search_facts", "Search the built knowledge database. Use it before add_fact to avoid duplicates.",
     _SEARCH_PROPS, ("terms",)),
    ("list_subjects", "List the subjects that exist, to file a new fact under an existing one.", _SUBJECTS_PROPS, ()),
    ("add_fact", "Record one researched fact. It is stored private and pending until a person approves it.",
     _ADD_FACT_PROPS, _ADD_FACT_REQUIRED),
)
TOOL_NAMES = tuple(t[0] for t in _TOOLS)
_SCHEMAS = {name: (props, required) for name, _, props, required in _TOOLS}


def tool_list():
    return [{"name": name, "description": description,
             "inputSchema": {"type": "object", "properties": props, "required": list(required),
                             "additionalProperties": False}}
            for name, description, props, required in _TOOLS]


# ------------------------------------------------------------------ envelopes

def result_message(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def error_message(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def encode(message):
    """One reply as one line (the stdio transport is newline-delimited, so no raw newline may appear)."""
    return json.dumps(message, ensure_ascii=True, default=str, separators=(",", ":"))


def decode(line):
    """(message, None) for a valid JSON-RPC request or notification, else (None, error reply)."""
    if not isinstance(line, str) or not line.strip():
        return None, error_message(None, PARSE_ERROR, "empty message")
    try:
        message = json.loads(line)
    except ValueError:
        return None, error_message(None, PARSE_ERROR, "message is not valid JSON")
    if not isinstance(message, dict):
        return None, error_message(None, INVALID_REQUEST, "a request must be a JSON object (batches are not supported)")
    id_ = message.get("id")
    if "id" in message and (isinstance(id_, bool) or not isinstance(id_, (str, int, type(None)))):
        return None, error_message(None, INVALID_REQUEST, "id must be a string or an integer")
    if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return None, error_message(id_, INVALID_REQUEST, "not a JSON-RPC 2.0 request")
    return message, None


def negotiate_version(params):
    asked = params.get("protocolVersion") if isinstance(params, dict) else None
    return asked if asked in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]


def initialize_result(params):
    return {"protocolVersion": negotiate_version(params), "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}, "instructions": INSTRUCTIONS}


# ------------------------------------------------------------------ tool results

def tool_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def rows_result(rows):
    return tool_result(json.dumps(list(rows), ensure_ascii=False, default=str, indent=1))


# ------------------------------------------------------------------ arguments

def _shape_problems(name, arguments):
    """Unknown keys, missing required ones and wrong types, as messages. Text must be non-blank and NUL-free."""
    props, required = _SCHEMAS[name]
    if not isinstance(arguments, dict):
        return ["arguments must be an object"]
    problems = [f"unknown argument {k!r}" for k in sorted(arguments) if k not in props]
    problems += [f"{k} is required" for k in required if k not in arguments]
    for key, value in arguments.items():
        spec = props.get(key)
        if spec is None:
            continue
        kind = spec["type"]
        if kind == "string":
            if not isinstance(value, str):
                problems.append(f"{key} must be text")
            elif not value.strip():
                problems.append(f"{key} must not be blank")
            elif "\x00" in value:
                problems.append(f"{key} must not contain NUL characters")
            elif len(value) > spec.get("maxLength", MAX_TEXT):
                problems.append(f"{key} is longer than {spec.get('maxLength', MAX_TEXT)} characters")
        elif kind == "boolean":
            if not isinstance(value, bool):
                problems.append(f"{key} must be true or false")
        elif kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                problems.append(f"{key} must be a whole number")
            elif not spec["minimum"] <= value <= spec["maximum"]:
                problems.append(f"{key} must be between {spec['minimum']} and {spec['maximum']}")
    return problems


def parse_add_fact(arguments, default_session_id):
    """(NewFact, []) or (None, problems). The server fixes provenance and review state; the rest is the caller's."""
    problems = _shape_problems("add_fact", arguments)
    if problems:
        return None, problems
    given = {k: arguments[k] for k in _STRING_FIELDS + _BOOL_FIELDS if k in arguments}
    given.setdefault("session_id", default_session_id)
    fact = NewFact(captured_via="mcp", status="pending", **given)
    errors = check_fact(fact)
    return (None, errors) if errors else (fact, [])


def parse_search(arguments):
    """((terms, filters), []) or (None, problems)."""
    problems = _shape_problems("search_facts", arguments)
    if problems:
        return None, problems
    filters = {"limit": arguments.get("limit", DEFAULT_LIMIT)}
    for key in ("subject", "include_pending"):
        if key in arguments:
            filters[key] = arguments[key]
    return (arguments["terms"], filters), []


def parse_subjects(arguments):
    problems = _shape_problems("list_subjects", arguments)
    return (None, problems) if problems else (bool(arguments.get("include_pending", False)), [])


# ------------------------------------------------------------------ outcomes

def problems_result(problems):
    return tool_result("Refused, nothing was written or read:\n- " + "\n- ".join(problems), is_error=True)


def add_fact_result(outcome):
    """The reply for a `RecordResult` (add_fact_service). Every way it can end is spelled out, so none is lost."""
    if outcome.refused is not None:
        return tool_result(f"Refused, nothing was written: {outcome.refused}", is_error=True)
    added = outcome.added
    notes = list(added.notes) if added is not None else []
    if added is None or not added.ok:
        errors = list(added.errors) if added is not None else ["no result from the write"]
        return tool_result("Refused, nothing was written:\n- " + "\n- ".join(errors + notes), is_error=True)
    lines = [f"Stored fact {(added.entry or {}).get('source_key', '?')} as {(added.entry or {}).get('visibility', 'private')} and pending review "
             f"({added.total} facts in the file). It appears in search after the next rebuild."]
    if added.privacy is not None and added.privacy.raised_above_request:
        lines.append(f"Stored as private although normal was requested: {added.privacy.explain()}")
    lines += notes
    if outcome.commit_error is not None:
        lines.append(f"The fact IS in the facts file but is NOT committed: {outcome.commit_error}")
        return tool_result("\n".join(lines), is_error=True)
    if outcome.repo is None:
        lines.append("The data file is not inside a git repository, so it was not committed.")
    else:
        lines.append(f"Committed {outcome.commit}.")
    return tool_result("\n".join(lines))
