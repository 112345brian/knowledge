"""Import Claude Code memory files as private, pending facts (issue #29).

Library only: nothing here prints or exits (the Typer command lives in cli_migrate.py).

Source: `<root>/*/memory/*.md`, root defaulting to ~/.claude/projects. Index files named
`MEMORY.md` are skipped and counted. Symlinked files and symlinked project folders are never
followed (a link could point anywhere): they are reported as `skipped-symlink`.

Frontmatter: `name`, `description` and `type` / `originSessionId` / `modified`, each found either
nested under `metadata:` or at the top level. A tiny YAML subset is parsed (scalars, one level of
nesting, `>`/`|` block scalars); no YAML dependency.

Mapping (documented defaults, nothing beyond them is assigned):
  * type `project` / `user` / `reference` -> one pending fact per file via add_fact.append_fact, so
    validation, the locked atomic append, the privacy resolver and provenance are reused:
      subject       claude-memory-<type>     (kebab-case; one subject per type, no per-project
                                              subjects, which would bake personal folder names
                                              into the subject tree)
      trust_level   unverified               (a memory file is a note Claude wrote, not a source)
      status        pending, visibility private (the resolver may only raise it), is_personal true
      statement     the body with whitespace collapsed; if longer than STATEMENT_MAX chars it is cut
                    at a word boundary and ends in " [...]". Nothing is lost: when cut, `notes`
                    holds the full body. An empty body falls back to `description`.
      source_quote  the first QUOTE_LINES non-empty body lines, capped at QUOTE_MAX chars
      notes         provenance (project folder / file name, name, description), the content hash
                    marker used for "changed since migration", and the full body when truncated
      captured_via  migrate-memory, session_id = originSessionId when present
      recheck_by    `modified` date + RECHECK_DAYS (180) as YYYY-MM-DD; with no usable `modified`,
                    today + RECHECK_DAYS and a warning on the file
  * type `feedback` -> skipped and listed (instructions to Claude, not facts)
  * any other / missing type, no or broken frontmatter, unreadable, non-UTF-8, NUL bytes, empty,
    or larger than MAX_FILE_BYTES (refused, never truncated) -> a `problem` row, never a crash.

Idempotence: source_key = "mm-" + first 16 hex of sha1("<project folder>/<file name>"), so the
identity is the file's place, not its content. A key already present in any entry file means the
file is `unchanged`, or `changed` (reported, NOT re-added) when its sha256 differs from the one
stored in the entry's notes. A run is serialized against other migrate runs with a lock file in
the system temp dir (keyed by the data file). It cannot be held across append_fact's own lock,
so a concurrent add_fact still just appends its own random-keyed entry.
"""
import contextlib
import glob
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

import add_fact
import clock
import private_git
import revisions
from add_fact import NewFact
from paths import PRIVATE_DATA_DIR

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


# ---------------------------------------------------------------- frontmatter

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


def read_memory_file(path, project, filename):
    mf = MemoryFile(project=project, filename=filename)
    try:
        size = os.stat(path).st_size
        if size > MAX_FILE_BYTES:
            mf.problem = f"file is {size} bytes, over the {MAX_FILE_BYTES}-byte limit (refused, not truncated)"
            return mf
        with open(path, "rb") as f:
            raw = f.read(MAX_FILE_BYTES + 1)
    except OSError as e:
        mf.problem = f"unreadable: {e.strerror or e}"
        return mf
    if len(raw) > MAX_FILE_BYTES:
        mf.problem = f"file is over the {MAX_FILE_BYTES}-byte limit (refused, not truncated)"
        return mf
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


# ---------------------------------------------------------------- mapping

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


def recheck_date(mf, today=None):
    """(YYYY-MM-DD, warning or None)."""
    base = parse_modified(mf.modified)
    warning = None
    if base is None:
        base = today or clock.now().date()
        warning = ("no `modified` in frontmatter" if not mf.modified else f"unrecognized `modified` format")
        warning += "; recheck_by counted from today"
    return (base + timedelta(days=RECHECK_DAYS)).isoformat(), warning


def build_fact(mf, today=None):
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
        # #7: a memory snapshot is a point-in-time copy of something that changes, and we always
        # compute a recheck_by (modified + RECHECK_DAYS), so 'volatile' with that recheck.
        volatility="volatile",
        is_personal=True, visibility="private", status="pending",
        notes="\n".join(notes), recheck_by=recheck,
        recheck_rationale="Claude Code memory snapshot; re-verify against the current state before relying on it",
        source_quote=make_quote(mf), captured_via=CAPTURED_VIA, session_id=mf.session_id,
        source_key=source_key_for(mf.project, mf.filename),
    )
    return fact, ([warning] if warning else []), truncated


# ---------------------------------------------------------------- run

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


def discover(root):
    """([(project, filename, path)], index_count, [(project, filename, why)]) sorted. Reads
    `<root>/*/memory/*.md`; links are listed in the third element, never opened."""
    root = os.path.abspath(os.path.expanduser(root))
    found, links, indexes = [], [], 0
    if not os.path.isdir(root):
        return found, indexes, links
    for project in sorted(os.listdir(root)):
        pdir = os.path.join(root, project)
        mdir = os.path.join(pdir, "memory")
        if os.path.islink(pdir) or os.path.islink(mdir):
            if os.path.isdir(mdir):
                links.append((project, "(memory folder)", "symlinked folder"))
            continue
        if not os.path.isdir(mdir):
            continue
        for path in sorted(glob.glob(os.path.join(glob.escape(mdir), "*.md"))):
            name = os.path.basename(path)
            if name == "MEMORY.md":
                indexes += 1
            elif os.path.islink(path):
                links.append((project, name, "symlink"))
            elif os.path.isfile(path):
                found.append((project, name, path))
    return found, indexes, links


def _lock_for(data_path):
    key = hashlib.sha1(os.path.realpath(data_path).encode()).hexdigest()
    directory = os.path.join(tempfile.gettempdir(), "knowledge-locks")
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, "migrate-memory-" + key + ".lock")


@contextlib.contextmanager
def _run_lock(data_path):
    f = open(_lock_for(data_path), "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        f.close()


def existing_entries(data_dir):
    """{source_key: notes or ''} across every entry file. Raises revisions.RevisionError."""
    out = {}
    for name, _ in revisions.ENTRY_FILES:
        path = os.path.join(data_dir, name)
        if not os.path.exists(path):
            continue
        for item in revisions._read_array(path):
            key = item.get("source_key")
            if isinstance(key, str):
                out[key] = item.get("notes") if isinstance(item.get("notes"), str) else ""
    return out


def migrate(root=None, data_path=None, db_path=None, dry_run=False, allow_dirty=False, today=None):
    """Plan and (unless dry_run) perform the import. Returns a MigrationReport; never prints or
    exits. Writes at most `data_path` and one commit of exactly that file."""
    root = DEFAULT_ROOT if root is None else root
    data_path = add_fact.DATA_PATH if data_path is None else data_path
    db_path = add_fact.DB_PATH if db_path is None else db_path
    report = MigrationReport(root=os.path.abspath(os.path.expanduser(root)), dry_run=dry_run)
    if not os.path.isdir(report.root):
        report.refused = f"memory root {report.root} is not a directory"
        return report
    found, report.index_files, links = discover(root)
    for project, name, why in links:
        report.files.append(FileResult(project, name, None, "skipped-symlink", why))

    lock = contextlib.nullcontext() if dry_run else _run_lock(data_path)
    with lock:
        try:
            existing = existing_entries(os.path.dirname(os.path.abspath(data_path)))
        except (revisions.RevisionError, OSError) as e:
            report.refused = f"cannot read the existing facts: {e}"
            return report
        todo = []   # (FileResult, NewFact)
        seen_keys = {}
        for project, name, path in found:
            mf = read_memory_file(path, project, name)
            key = source_key_for(project, name)
            res = FileResult(project, name, mf.type, "problem", source_key=key)
            report.files.append(res)
            if mf.problem:
                res.detail = mf.problem
                continue
            if mf.type in SKIPPED_TYPES:
                res.action, res.detail = "skipped-feedback", "feedback files are instructions to Claude, not facts"
                continue
            if mf.type not in FACT_TYPES:
                res.detail = f"unrecognized or missing type {mf.type!r}" if mf.type else "no type in frontmatter"
                continue
            if key in seen_keys:
                res.detail = f"derived source_key collides with {seen_keys[key]}"
                continue
            seen_keys[key] = f"{project}/{name}"
            if key in existing:
                m = HASH_RE.search(existing[key])
                if m and m.group(1) != mf.sha256:
                    res.action, res.detail = "changed", "changed since migration (not re-added)"
                else:
                    res.action, res.detail = "unchanged", "already migrated"
                continue
            if not (_collapse(mf.body) or _collapse(mf.description or "")):
                res.detail = "empty body and description"
                continue
            fact, warnings, truncated = build_fact(mf, today)
            res.warnings = warnings
            errors, _ = add_fact.validate_fact(fact, db_path)
            if errors:
                res.detail = "; ".join(errors)
                continue
            res.action = "would-add"
            res.detail = "statement cut, full text in notes" if truncated else ""
            todo.append((res, fact))

        if dry_run or not todo:
            return report

        repo = None
        try:
            repo = private_git.find_repo(os.path.dirname(os.path.abspath(data_path)))
            if repo is None:
                report.not_in_git = True
            elif not allow_dirty:
                private_git.ensure_clean_tree(repo)
        except private_git.PrivateGitError as e:
            report.refused = str(e)
            for res, _ in todo:
                res.action, res.detail = "problem", "not written: run refused"
            return report

        written = 0
        try:
            for res, fact in todo:
                result = add_fact.append_fact(fact, data_path=data_path, db_path=db_path)
                if not result.ok:
                    res.action, res.detail = "problem", "; ".join(result.errors)
                    continue
                res.action = "added"
                written += 1
        finally:
            if written and repo is not None:
                message = f"migrate-memory: {written} pending fact{'s' if written != 1 else ''}"
                try:
                    report.commit = private_git.commit_private_change([data_path], message, repo)
                    report.detached = private_git.is_detached(repo)
                except private_git.PrivateGitError as e:
                    report.commit_error = str(e)
    return report
