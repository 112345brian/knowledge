#!/usr/bin/env python3
"""Append one ad hoc fact to general_facts.json (in knowledge-private, not
this repo), enforcing the shape 11_seed_general_facts.py expects (valid
trust_level, kebab-case subject name, a real source citekey if one is
given). This is the sanctioned way to add a "raw", not-project-specific
fact -- one with no vault or bodybuilding project behind it -- without
hand-editing the JSON or the live db.

Doesn't touch knowledge.db itself: rerun `python3 build.py` afterward to
pick the new fact up, same as any other data/*.json change.

Usage:
    python3 add_fact.py "Statement text." --subject some-subject --trust medium
    python3 add_fact.py "..." --subject x --trust high --domain general \
        --notes "..." --recheck-by 2026-12-01 --recheck-rationale "..." \
        --source-citekey some-existing-citekey --source-locator "p. 4"
"""
import argparse, contextlib, hashlib, json, os, re, sqlite3, stat, sys, tempfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

import clock
from paths import KNOWLEDGE_DB_DIR, PRIVATE_DATA_DIR
from private_git import PrivateGitError, commit_private_change, ensure_clean_tree, find_repo, is_detached

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(PRIVATE_DATA_DIR, "general_facts.json")
DB_PATH = os.path.join(os.path.expanduser(KNOWLEDGE_DB_DIR), "knowledge.db")
VALID_TRUST = {"verified", "high", "medium", "low", "unverified", "disputed"}
VALID_VISIBILITY = {"private", "normal"}  # keep in sync with the CHECK on facts.visibility
SUBJECT_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VIA_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("statement", help="The fact itself, as a full sentence.")
    p.add_argument("--subject", required=True, help="kebab-case subject name, e.g. 'car-maintenance'. Reused if it already exists, otherwise created.")
    p.add_argument("--trust", required=True, choices=sorted(VALID_TRUST), dest="trust_level")
    p.add_argument("--domain", default="general", help="Only applies if --subject doesn't exist yet (default: general).")
    p.add_argument("--original-claim", action="store_true", dest="is_original_claim", help="This is your own conclusion, not something a source states.")
    p.add_argument("--not-personal", action="store_false", dest="is_personal", help="Mark as not about the user personally (default: personal).")
    p.add_argument("--visibility", default="private", help="private (default) or normal. Only 'normal' facts may leave the local machine; when unsure, leave it private.")
    p.add_argument("--trust-rationale")
    p.add_argument("--notes")
    p.add_argument("--recheck-by", help="A date (YYYY-MM-DD) or short phrase like 'next physical'.")
    p.add_argument("--recheck-rationale")
    p.add_argument("--source-citekey", help="Must already exist in the `sources` table.")
    p.add_argument("--source-locator")
    p.add_argument("--source-quote", help="The words that justified the fact (with --captured-via, no --source-citekey is needed).")
    p.add_argument("--captured-via", help="Where the fact came from: cli, mcp, migrate-memory, ... With 'mcp', --session-id and --source-quote are required.")
    p.add_argument("--session-id", help="The conversation/session the fact was captured in.")
    p.add_argument("--allow-dirty", action="store_true", help="Skip the clean-tree check on knowledge-private (deliberate batch edits only); the commit still contains only the facts file.")
    p.set_defaults(is_personal=True)
    return p.parse_args(argv)


@dataclass
class NewFact:
    """One fact to add. Mirrors the CLI flags so the CLI, the MCP tools and tests share one shape."""
    statement: str
    subject: str
    trust_level: str
    domain: str = "general"
    is_original_claim: bool = False
    is_personal: bool = True
    visibility: str = "private"
    trust_rationale: Optional[str] = None
    notes: Optional[str] = None
    recheck_by: Optional[str] = None
    recheck_rationale: Optional[str] = None
    source_citekey: Optional[str] = None
    source_locator: Optional[str] = None
    source_quote: Optional[str] = None
    # Provenance. All optional except that captured_via="mcp" requires session_id and source_quote.
    # captured_at is stamped from the clock when captured_via is set and it is left None.
    captured_via: Optional[str] = None
    session_id: Optional[str] = None
    captured_at: Optional[str] = None


@dataclass
class AddResult:
    ok: bool
    errors: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    entry: Optional[dict] = None
    total: Optional[int] = None


class DataFileError(Exception):
    """The facts file exists but can't be safely appended to."""


def _is_utc_offset_timestamp(value):
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value).utcoffset() is not None
    except ValueError:
        return False


def validate_fact(fact, db_path=None):
    """Returns (errors, notes). Never prints. Same rules and messages the CLI has always had."""
    db_path = DB_PATH if db_path is None else db_path
    errors, notes = [], []
    if fact.trust_level not in VALID_TRUST:
        errors.append(f"trust_level {fact.trust_level!r} must be one of {sorted(VALID_TRUST)}")
    if not isinstance(fact.visibility, str) or fact.visibility not in VALID_VISIBILITY:
        errors.append(f"visibility {fact.visibility!r} must be one of {sorted(VALID_VISIBILITY)}")
    if not fact.statement.strip():
        errors.append("statement is empty")
    if not SUBJECT_RE.match(fact.subject):
        errors.append(f"--subject {fact.subject!r} must be lowercase kebab-case (e.g. 'car-maintenance')")
    if fact.recheck_by and DATE_RE.match(fact.recheck_by) is None and len(fact.recheck_by) < 4:
        errors.append(f"--recheck-by {fact.recheck_by!r} looks too short to be a date or phrase")
    if fact.captured_via is not None:
        if not isinstance(fact.captured_via, str) or not VIA_RE.match(fact.captured_via):
            errors.append(f"captured_via {fact.captured_via!r} must be a lowercase kebab-case token (e.g. 'mcp', 'cli', 'migrate-memory')")
    if fact.captured_via is None and (fact.session_id or fact.captured_at):
        errors.append("session_id/captured_at given without captured_via")
    if fact.captured_via == "mcp":
        for name in ("session_id", "source_quote"):
            value = getattr(fact, name)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{name} is required when captured_via is 'mcp'")
    if fact.captured_at is not None and not _is_utc_offset_timestamp(fact.captured_at):
        errors.append(f"captured_at {fact.captured_at!r} must be an ISO-8601 timestamp with a UTC offset (e.g. 2026-10-03T08:00:00+00:00)")
    # A quote needs a citekey to hang off, unless the fact carries provenance: then the quote is
    # the user's own words and lives on the fact itself.
    if fact.source_locator and not fact.source_citekey:
        errors.append("--source-locator/--source-quote given without --source-citekey")
    elif fact.source_quote and not fact.source_citekey and fact.captured_via is None:
        errors.append("--source-locator/--source-quote given without --source-citekey")
    if fact.source_citekey:
        if not os.path.exists(db_path):
            notes.append(f"  (skipping citekey check -- {db_path} doesn't exist yet)")
        else:
            try:
                con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                try:
                    row = con.execute("SELECT 1 FROM sources WHERE citekey = ?", (fact.source_citekey,)).fetchone()
                finally:
                    con.close()
            except sqlite3.Error as e:
                errors.append(f"could not check --source-citekey against {db_path}: {e}")
            else:
                if not row:
                    errors.append(f"--source-citekey {fact.source_citekey!r} not found in sources table")
    return errors, notes


def build_entry(fact):
    """The JSON entry 11_seed_general_facts.py expects. Key order is part of the file format."""
    entry = {
        "subject": fact.subject,
        "statement": fact.statement.strip(),
        "trust_level": fact.trust_level,
        "is_original_claim": bool(fact.is_original_claim),
        "is_personal": bool(fact.is_personal),
        "date_added": clock.now_iso(),
        "visibility": fact.visibility,
    }
    if fact.domain != "general":
        entry["domain"] = fact.domain
    for key in ("trust_rationale", "notes", "recheck_by", "recheck_rationale",
                "source_citekey", "source_locator", "source_quote"):
        value = getattr(fact, key)
        if value:
            entry[key] = value
    if fact.captured_via:
        entry["captured_via"] = fact.captured_via
        if fact.session_id:
            entry["session_id"] = fact.session_id
        entry["captured_at"] = fact.captured_at or clock.now_iso()
    return entry


def _lock_path(path):
    """Lock sidecar in the system temp dir, keyed by the data file's real path, so no lock file
    ever appears inside knowledge-private (it would break the clean-tree check in issue #10)."""
    key = hashlib.sha1(os.path.realpath(path).encode()).hexdigest()
    directory = os.path.join(tempfile.gettempdir(), "knowledge-locks")
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, key + ".lock")


@contextlib.contextmanager
def _file_lock(lock_path):
    """Exclusive inter-process lock on a sidecar file (fcntl on POSIX, msvcrt on Windows)."""
    f = open(lock_path, "a+")
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
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            f.close()


def _read_array(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            items = json.load(f)
    except json.JSONDecodeError as e:
        raise DataFileError(f"{path} is not valid JSON ({e}); left untouched") from e
    if not isinstance(items, list):
        raise DataFileError(f"{path} must contain a JSON array; left untouched")
    return items


def _atomic_write_json(path, items):
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))
        else:
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(tmp, 0o666 & ~umask)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def append_record(path, record):
    """Append one record to a JSON array file: locked, read inside the lock, written to a temp
    file and swapped in with os.replace, so a crash leaves the old file or the new one, never half.
    Generic on purpose -- other append-only JSON files reuse it. Returns the new total."""
    with _file_lock(_lock_path(path)):
        items = _read_array(path)
        items.append(record)
        _atomic_write_json(path, items)
    return len(items)


def append_fact(fact, data_path=None, db_path=None):
    """Validate and append one fact. Returns an AddResult; never prints or exits."""
    data_path = DATA_PATH if data_path is None else data_path
    errors, notes = validate_fact(fact, db_path)
    if errors:
        return AddResult(ok=False, errors=errors, notes=notes)
    entry = build_entry(fact)
    try:
        total = append_record(data_path, entry)
    except DataFileError as e:
        return AddResult(ok=False, errors=[str(e)], notes=notes)
    except OSError as e:
        return AddResult(ok=False, errors=[f"could not write {data_path}: {e}"], notes=notes)
    return AddResult(ok=True, notes=notes, entry=entry, total=total)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    fact = NewFact(
        statement=args.statement, subject=args.subject, trust_level=args.trust_level, domain=args.domain,
        is_original_claim=args.is_original_claim, is_personal=args.is_personal, visibility=args.visibility,
        trust_rationale=args.trust_rationale, notes=args.notes, recheck_by=args.recheck_by,
        recheck_rationale=args.recheck_rationale, source_citekey=args.source_citekey,
        source_locator=args.source_locator, source_quote=args.source_quote,
        captured_via=args.captured_via, session_id=args.session_id,
    )
    # Git safety net (#10): only when the data file lives in a git repo (a non-git data dir,
    # e.g. a throwaway test layout, is written without committing, with a note). append_fact
    # itself stays free of git side effects.
    try:
        repo = find_repo(os.path.dirname(DATA_PATH))
        if repo is None:
            print(f"note: {os.path.dirname(DATA_PATH)} is not inside a git repository; the change will not be committed.", file=sys.stderr)
        elif not args.allow_dirty:
            ensure_clean_tree(repo)
    except PrivateGitError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    result = append_fact(fact)
    for note in result.notes:
        print(note, file=sys.stderr)
    if not result.ok:
        for e in result.errors:
            print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"Added to {os.path.relpath(DATA_PATH, HERE)} ({result.total} facts total). Run `python3 build.py` to rebuild knowledge.db.")
    if repo is not None:
        message = f"add-fact: {fact.subject} ({fact.trust_level})"
        try:
            commit = commit_private_change([DATA_PATH], message, repo)
            detached = is_detached(repo)
        except PrivateGitError as e:
            print(f"error: the fact IS in {DATA_PATH} but is NOT committed: {e}", file=sys.stderr)
            return 3
        print(f"Committed {commit} in {repo}: {message}")
        if detached:
            print(f"warning: {repo} has a detached HEAD; that commit is not on any branch.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
