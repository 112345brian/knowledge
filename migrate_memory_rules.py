"""Import Claude Code memory files as private, pending facts (issue #29), the rules (domain: no file,
db, git, clock or lock).

Parsing of a memory file's text and frontmatter, the mapping to a `NewFact`, the per-file decision
(add / unchanged / changed / skip / problem) and the report types. `migrate_memory_store` finds and
reads the files and holds the run lock; `migrate_memory` is the use case that validates, appends
through add_fact and commits. See migrate_memory.py for the full mapping and its documented defaults.
"""
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

from new_fact import NewFact

CAPTURED_VIA = "migrate-memory"
DEFAULT_ROOT = "~/.claude/projects"
FACT_TYPES = ("project", "user", "reference")
SKIPPED_TYPES = ("feedback",)
MAX_FILE_BYTES = 64 * 1024
STATEMENT_MAX = 600
QUOTE_LINES = 5
QUOTE_MAX = 600
RECHECK_DAYS = 180
HASH_MARKER = "memory-content-sha256: "
HASH_RE = re.compile(r"^" + re.escape(HASH_MARKER) + r"([0-9a-f]{64})$", re.M)

ACTIONS = ("added", "would-add", "unchanged", "changed", "skipped-feedback", "skipped-symlink", "problem")


@dataclass
class MemoryFile:
    project: str
    filename: str
    type: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    session_id: Optional[str] = None
    modified: Optional[str] = None
    body: str = ""
    sha256: str = ""
    problem: Optional[str] = None


def _unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def parse_frontmatter(text):
    """(fields, body) or raises ValueError. `fields` maps top-level keys to a string, or to a dict of
    strings for a nested mapping."""
    lines = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValueError("no frontmatter (file does not start with ---)")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise ValueError("frontmatter is not closed (no second ---)")
    fields, key, block = {}, None, None
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        top = re.match(r"^([A-Za-z_][\w-]*):[ \t]*(.*)$", line)
        if top:
            key, value = top.group(1), top.group(2).strip()
            block = None
            if value == "":
                fields[key] = {}
            elif re.fullmatch(r"[>|][+-]?", value):
                fields[key], block = [], key
            else:
                fields[key] = _unquote(value)
            continue
        if key is None:
            continue
        if block is not None:
            fields[block].append(line.strip())
            continue
        child = re.match(r"^\s+([A-Za-z_][\w-]*):[ \t]*(.*)$", line)
        if child and isinstance(fields[key], dict):
            fields[key][child.group(1)] = _unquote(child.group(2))
        elif isinstance(fields[key], str):  # folded plain scalar continuation
            fields[key] += " " + line.strip()
    for k, v in list(fields.items()):
        if isinstance(v, list):
            fields[k] = " ".join(x for x in v if x)
    return fields, "\n".join(lines[end + 1:]).strip()


def _field(fields, name):
    """`name` from the top level, else from `metadata:`; blank counts as absent."""
    for scope in (fields, fields.get("metadata") if isinstance(fields.get("metadata"), dict) else {}):
        v = scope.get(name)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def parse_modified(value):
    """A date from `modified`: ISO date, ISO datetime (Z, offset, T or space), or None. A datetime
    with an offset is converted to UTC first so the date does not depend on the machine."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        try:
            return date.fromisoformat(v)
        except ValueError:
            return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.date()


def parse_memory_bytes(raw, mf):
    """Fill `mf` (a MemoryFile with project and filename) from the file's bytes, or set mf.problem. The
    size limit is enforced by the reader before this is called."""
    mf.sha256 = hashlib.sha256(raw).hexdigest()
    if b"\x00" in raw:
        mf.problem = "contains NUL bytes"
        return mf
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        mf.problem = "not valid UTF-8"
        return mf
    try:
        fields, body = parse_frontmatter(text)
    except ValueError as e:
        mf.problem = str(e)
        return mf
    mf.body = body
    mf.name = _field(fields, "name")
    mf.description = _field(fields, "description")
    mf.session_id = _field(fields, "originSessionId")
    mf.modified = _field(fields, "modified")
    t = _field(fields, "type")
    mf.type = t.lower() if t else None
    return mf


def source_key_for(project, filename):
    return "mm-" + hashlib.sha1(f"{project}/{filename}".encode("utf-8", "surrogatepass")).hexdigest()[:16]


def subject_for(type_):
    return f"claude-memory-{type_}"


def _collapse(text):
    return re.sub(r"\s+", " ", text).strip()


def make_statement(mf):
    """(statement, truncated). Body first, description when the body is empty."""
    text = _collapse(mf.body) or _collapse(mf.description or "")
    if len(text) <= STATEMENT_MAX:
        return text, False
    cut = text[:STATEMENT_MAX - 6]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + " [...]", True


def make_quote(mf):
    lines = [l.strip() for l in mf.body.splitlines() if l.strip()][:QUOTE_LINES]
    return "\n".join(lines)[:QUOTE_MAX] or None


def recheck_date(mf, today):
    """(YYYY-MM-DD, warning or None)."""
    base = parse_modified(mf.modified)
    warning = None
    if base is None:
        base = today
        warning = ("no `modified` in frontmatter" if not mf.modified else f"unrecognized `modified` format")
        warning += "; recheck_by counted from today"
    return (base + timedelta(days=RECHECK_DAYS)).isoformat(), warning


def build_fact(mf, today):
    """(NewFact, warnings, truncated) for a file whose type is in FACT_TYPES and that has content."""
    statement, truncated = make_statement(mf)
    recheck, warning = recheck_date(mf, today)
    notes = [f"migrated from Claude Code memory: {mf.project}/{mf.filename}"]
    if mf.name:
        notes.append(f"name: {mf.name}")
    if mf.description:
        notes.append(f"description: {mf.description}")
    notes.append(f"memory type: {mf.type}; modified: {mf.modified or 'unknown'}")
    notes.append(HASH_MARKER + mf.sha256)
    if truncated:
        notes.append("full text (statement is cut):\n" + mf.body)
    fact = NewFact(
        statement=statement, subject=subject_for(mf.type), trust_level="unverified",
        # #7: we always compute a recheck_by (modified + RECHECK_DAYS), so the fact is 'recheck'.
        is_personal=True, visibility="private", status="pending",
        notes="\n".join(notes), recheck_by=recheck,
        recheck_rationale="Claude Code memory snapshot; re-verify against the current state before relying on it",
        source_quote=make_quote(mf), captured_via=CAPTURED_VIA, session_id=mf.session_id,
        source_key=source_key_for(mf.project, mf.filename),
    )
    return fact, ([warning] if warning else []), truncated


@dataclass
class FileResult:
    project: str
    file: str
    type: Optional[str]
    action: str
    detail: str = ""
    source_key: Optional[str] = None
    warnings: List[str] = field(default_factory=list)

    def to_dict(self):
        return {"project": self.project, "file": self.file, "type": self.type, "action": self.action,
                "detail": self.detail, "source_key": self.source_key, "warnings": self.warnings}


@dataclass
class MigrationReport:
    root: str
    dry_run: bool
    files: List[FileResult] = field(default_factory=list)
    index_files: int = 0
    refused: Optional[str] = None       # whole run refused before writing (dirty tree, unreadable data file...)
    commit: Optional[str] = None
    commit_error: Optional[str] = None
    detached: bool = False
    not_in_git: bool = False

    def counts(self):
        """{type or '(none)': {action: n}}"""
        out: Dict[str, Dict[str, int]] = {}
        for r in self.files:
            row = out.setdefault(r.type or "(none)", {})
            row[r.action] = row.get(r.action, 0) + 1
        return out

    def totals(self):
        t = {a: 0 for a in ACTIONS}
        for r in self.files:
            t[r.action] += 1
        return t

    @property
    def problems(self):
        return [r for r in self.files if r.action == "problem"]

    @property
    def exit_code(self):
        if self.commit_error:
            return 3
        return 1 if (self.refused or self.problems) else 0

    def to_dict(self):
        return {"root": self.root, "dry_run": self.dry_run, "totals": self.totals(), "by_type": self.counts(),
                "index_files_skipped": self.index_files, "files": [r.to_dict() for r in self.files],
                "refused": self.refused, "commit": self.commit, "commit_error": self.commit_error,
                "not_in_git": self.not_in_git, "detached": self.detached, "exit_code": self.exit_code}


def plan_file(mf, project, name, existing, seen_keys, today):
    """Decide what to do with one memory file: (FileResult, NewFact | None, truncated). The FileResult
    is final unless a NewFact is returned (then the caller validates it and marks it would-add).
    `existing` is {source_key: notes}, `seen_keys` {key: "project/name"} of this run (updated)."""
    key = source_key_for(project, name)
    res = FileResult(project, name, mf.type, "problem", source_key=key)
    if mf.problem:
        res.detail = mf.problem
        return res, None, False
    if mf.type in SKIPPED_TYPES:
        res.action, res.detail = "skipped-feedback", "feedback files are instructions to Claude, not facts"
        return res, None, False
    if mf.type not in FACT_TYPES:
        res.detail = f"unrecognized or missing type {mf.type!r}" if mf.type else "no type in frontmatter"
        return res, None, False
    if key in seen_keys:
        res.detail = f"derived source_key collides with {seen_keys[key]}"
        return res, None, False
    seen_keys[key] = f"{project}/{name}"
    if key in existing:
        m = HASH_RE.search(existing[key])
        if m and m.group(1) != mf.sha256:
            res.action, res.detail = "changed", "changed since migration (not re-added)"
        else:
            res.action, res.detail = "unchanged", "already migrated"
        return res, None, False
    if not (_collapse(mf.body) or _collapse(mf.description or "")):
        res.detail = "empty body and description"
        return res, None, False
    fact, warnings, truncated = build_fact(mf, today)
    res.warnings = warnings
    return res, fact, truncated
