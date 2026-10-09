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
    python3 add_fact.py "Statement text." --subject some-subject --trust medium \
        --recheck-by 2027-01-01
    python3 add_fact.py "..." --subject x --trust medium --no-decay \\
        --recheck-rationale "a birthdate does not change"
    python3 add_fact.py "..." --subject x --trust high --domain general \
        --notes "..." --recheck-by 2026-12-01 --recheck-rationale "..." \
        --source-citekey some-existing-citekey --source-locator "p. 4"

Freshness is mandatory (#7). A new fact needs EITHER --recheck-by (a date or short phrase) OR
--no-decay together with --recheck-rationale saying why it does not decay; neither, or both, is
refused. --no-decay is NOT an excuse to default --trust to 'verified': the fact still needs an
honestly considered trust level (cross-checked against an ID vs. typed from memory). The schema
only enforces that one of the two is present; it cannot judge whether a fact really does not
decay. Staleness only: a fact that was wrong when typed is --trust's job.
"""
import argparse, contextlib, hashlib, json, os, re, sqlite3, stat, sys, tempfile, uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

import clock
import fixity
import privacy
import validtime
from paths import KNOWLEDGE_DB_DIR, PRIVATE_DATA_DIR
from private_git import PrivateGitError, commit_private_change, ensure_clean_tree, find_repo, is_detached

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(PRIVATE_DATA_DIR, "general_facts.json")
DB_PATH = os.path.join(os.path.expanduser(KNOWLEDGE_DB_DIR), "knowledge.db")
VALID_TRUST = {"verified", "high", "medium", "low", "unverified", "disputed"}
VALID_VISIBILITY = {"private", "normal"}  # keep in sync with the CHECK on facts.visibility
# #7: every fact has a freshness (facts.freshness). A NEW fact derives 'recheck' (it has a
# recheck_by) or 'no-decay' (explicit assertion plus a written recheck_rationale); 'unreviewed' is
# legacy-only (facts that predate the column and were never reviewed) and is never accepted here.
FRESHNESS_VALUES = ("recheck", "no-decay", "unreviewed")  # keep in sync with the CHECK in schema.sql
# A new fact starts 'pending' (awaiting review, #6) unless the caller already reviewed it
# ('active', e.g. `register facts`, #32). superseded/retracted only arise through revisions.
VALID_NEW_STATUS = ("pending", "active")
# #39: what kind of assertion a fact is. Self-reported guidance (the schema only enforces the vocabulary,
# it cannot judge whether a fact really is a decision or an observation). 'unclassified' is the honest
# marker for a fact nobody classified (every legacy fact, and the default for a new one).
# Keep in sync with the CHECKs on facts.kind / fact_revisions.kind in schema.sql and normal_db.py.
KIND_VALUES = ("observation", "measurement", "decision", "preference", "plan", "definition",
               "inference", "rule", "lesson", "unclassified")
SUBJECT_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VIA_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
# Stable identity of a fact across rebuilds (#30): revisions in fact_revisions.jsonl point at it.
SOURCE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def new_source_key():
    return "f-" + uuid.uuid4().hex[:12]


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("statement", help="The fact itself, as a full sentence.")
    p.add_argument("--subject", required=True, help="kebab-case subject name, e.g. 'car-maintenance'. Reused if it already exists, otherwise created.")
    p.add_argument("--trust", required=True, choices=sorted(VALID_TRUST), dest="trust_level")
    p.add_argument("--domain", default="general", help="Only applies if --subject doesn't exist yet (default: general).")
    p.add_argument("--original-claim", action="store_true", dest="is_original_claim", help="This is your own conclusion, not something a source states.")
    p.add_argument("--not-personal", action="store_false", dest="is_personal", help="Mark as not about the user personally (default: personal).")
    p.add_argument("--no-decay", action="store_true", dest="no_decay",
                   help="Assert this fact does not decay (a birthdate, a completed purchase); requires --recheck-rationale "
                        "and excludes --recheck-by. It does NOT justify --trust verified.")
    p.add_argument("--kind", default="unclassified", choices=KIND_VALUES,
                   help="What kind of assertion this is (default: unclassified). Self-reported guidance.")
    p.add_argument("--valid-from", dest="valid_from", help="When the fact became true: YYYY, YYYY-MM or YYYY-MM-DD (#40). Omit if unknown.")
    p.add_argument("--valid-to", dest="valid_to", help="When it stopped being true (same formats; same value as --valid-from for a point in time). Omit if still true as far as known.")
    p.add_argument("--visibility", default="private", help="private (default) or normal. Only 'normal' facts may leave the local machine; when unsure, leave it private.")
    p.add_argument("--trust-rationale")
    p.add_argument("--notes")
    p.add_argument("--recheck-by", help="A date (YYYY-MM-DD) or short phrase like 'next physical'. Required unless --no-decay.")
    p.add_argument("--recheck-rationale", help="Why this recheck date; with --no-decay, why the fact does not decay (required).")
    p.add_argument("--source-citekey", help="Must already exist in the `sources` table.")
    p.add_argument("--source-locator")
    p.add_argument("--origin-path", help="The file this fact was extracted from; its SHA-256 is recorded as the fixity baseline (#38) when the file is readable.")
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
    # #7: every new fact needs a recheck_by OR no_decay=True plus a recheck_rationale (neither or
    # both is refused). Freshness is derived from these, never passed in, so a caller cannot
    # supply the legacy-only 'unreviewed'.
    no_decay: bool = False
    domain: str = "general"
    is_original_claim: bool = False
    is_personal: bool = True
    visibility: str = "private"
    kind: str = "unclassified"   # #39, one of KIND_VALUES
    # Valid time (#40): when the fact was true. YYYY / YYYY-MM / YYYY-MM-DD or None; see validtime.py.
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    trust_rationale: Optional[str] = None
    notes: Optional[str] = None
    recheck_by: Optional[str] = None
    recheck_rationale: Optional[str] = None
    source_citekey: Optional[str] = None
    source_locator: Optional[str] = None
    source_quote: Optional[str] = None
    # The file the fact was extracted from (#38). Its SHA-256 is written to the entry as
    # `extracted_from_sha256` when the file is readable; an unreadable file records no hash.
    origin_path: Optional[str] = None
    # Provenance. All optional except that captured_via="mcp" requires session_id and source_quote.
    # captured_at is stamped from the clock when captured_via is set and it is left None.
    captured_via: Optional[str] = None
    session_id: Optional[str] = None
    captured_at: Optional[str] = None
    # Stable identity (#30). Normally left None: build_entry assigns a fresh one.
    source_key: Optional[str] = None
    # Review state (#6). Ad hoc facts start 'pending' and become active via review.approve; a caller
    # that already showed the fact to the user passes 'active'. Always written to the entry.
    status: str = "pending"


@dataclass
class AddResult:
    ok: bool
    errors: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    entry: Optional[dict] = None
    total: Optional[int] = None
    # #31: how the stored visibility was derived. entry["visibility"] is the resolved value, which
    # may be stricter than NewFact.visibility; privacy.explain() says which rule raised it.
    privacy: Optional["privacy.Resolution"] = None


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
    if not isinstance(fact.kind, str) or fact.kind not in KIND_VALUES:
        errors.append(f"kind {fact.kind!r} must be one of {list(KIND_VALUES)}")
    errors.extend(validtime.problems(fact.valid_from, fact.valid_to))
    if not isinstance(fact.visibility, str) or fact.visibility not in VALID_VISIBILITY:
        errors.append(f"visibility {fact.visibility!r} must be one of {sorted(VALID_VISIBILITY)}")
    if not isinstance(fact.status, str) or fact.status not in VALID_NEW_STATUS:
        errors.append(f"status {fact.status!r} must be one of {list(VALID_NEW_STATUS)} for a new fact")
    if not isinstance(fact.no_decay, bool):
        errors.append(f"no_decay {fact.no_decay!r} must be true or false")
    else:
        has_recheck = isinstance(fact.recheck_by, str) and bool(fact.recheck_by.strip())
        if has_recheck and fact.no_decay:
            errors.append("recheck_by and no_decay contradict each other: give a recheck_by (the fact decays) or "
                          "no_decay with a recheck_rationale (it does not), not both")
        elif not has_recheck and not fact.no_decay:
            errors.append("a fact needs freshness: give recheck_by (a date or short phrase), or no_decay together "
                          "with a recheck_rationale saying why it does not decay")
        elif fact.no_decay and not (isinstance(fact.recheck_rationale, str) and fact.recheck_rationale.strip()):
            errors.append("no_decay requires a non-blank recheck_rationale saying why the fact does not decay")
    if not fact.statement.strip():
        errors.append("statement is empty")
    if not SUBJECT_RE.match(fact.subject):
        errors.append(f"--subject {fact.subject!r} must be lowercase kebab-case (e.g. 'car-maintenance')")
    if isinstance(fact.recheck_by, str) and fact.recheck_by.strip() and DATE_RE.match(fact.recheck_by) is None and len(fact.recheck_by) < 4:
        errors.append(f"--recheck-by {fact.recheck_by!r} looks too short to be a date or phrase")
    if fact.source_key is not None and (not isinstance(fact.source_key, str) or not SOURCE_KEY_RE.match(fact.source_key)):
        errors.append(f"source_key {fact.source_key!r} must match {SOURCE_KEY_RE.pattern}")
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


def build_entry(fact, visibility=None):
    """The JSON entry 11_seed_general_facts.py expects. Key order is part of the file format.
    `visibility` is the resolved value from privacy.resolve_visibility (append_fact passes it);
    left None it falls back to the caller's request."""
    entry = {
        "source_key": fact.source_key or new_source_key(),
        "subject": fact.subject,
        "statement": fact.statement.strip(),
        "trust_level": fact.trust_level,
        "is_original_claim": bool(fact.is_original_claim),
        "is_personal": bool(fact.is_personal),
        "date_added": clock.now_iso(),
        "visibility": fact.visibility if visibility is None else visibility,
        "status": fact.status,
        "freshness": "no-decay" if fact.no_decay else "recheck",
        "kind": fact.kind,
    }
    for key in ("valid_from", "valid_to"):
        if getattr(fact, key) is not None:
            entry[key] = getattr(fact, key)
    if fact.domain != "general":
        entry["domain"] = fact.domain
    for key in ("trust_rationale", "notes", "recheck_by", "recheck_rationale",
                "source_citekey", "source_locator", "source_quote"):
        value = getattr(fact, key)
        if key == "recheck_by" and isinstance(value, str) and not value.strip():
            continue  # blank counts as absent (a no_decay fact may carry a blank one)
        if value:
            entry[key] = value
    if fact.origin_path and fact.origin_path.strip():
        entry["origin_path"] = fact.origin_path.strip()
        fp = fixity.fingerprint(entry["origin_path"])
        if fp["content_sha256"]:
            entry["extracted_from_sha256"] = fp["content_sha256"]
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
    # Created with mode 0o666 so the KERNEL applies the umask (a plain new file's default). Reading the
    # umask with os.umask(0) would change it for the whole process, and other threads creating files
    # in that window would get the wrong mode.
    tmp = f"{os.path.abspath(path)}.{uuid.uuid4().hex}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))  # an existing file keeps its mode
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


def append_records(path, records, reject=None):
    """Append several records to a JSON array file in ONE locked read-modify-write (one atomic
    swap), for batch callers (facts_batch.add_facts, #32). Same lock, read and write as
    append_record, so it serialises with add_fact and with other batches.

    `reject(existing, record)`, if given, runs INSIDE the lock for each record in order
    (`existing` already holds the file's records plus the records accepted earlier in this call)
    and returns a reason string to skip that record, or None to accept it. Nothing is written when
    no record is accepted. Returns (total, rejected) where `rejected` is a list of
    (index_in_records, reason) and `total` is the length of the file afterwards."""
    with _file_lock(_lock_path(path)):
        items = _read_array(path)
        rejected, accepted = [], 0
        for index, record in enumerate(records):
            reason = reject(items, record) if reject is not None else None
            if reason:
                rejected.append((index, reason))
            else:
                items.append(record)
                accepted += 1
        if accepted:
            _atomic_write_json(path, items)
    return len(items), rejected


def resolve_privacy(fact, data_path, db_path):
    """The privacy rules (privacy_rules.json next to the data file) applied to one fact. Subject
    context comes from the db when it has a subjects table (parents for tag inheritance, and the set
    of known subjects) plus subjects already in the facts file; with no usable db the unknown-subject
    rule is not enforced (nothing to compare against). Raises privacy.PrivacyRulesError."""
    rules = privacy.load_rules(os.path.join(os.path.dirname(os.path.abspath(data_path)), privacy.RULES_FILENAME))
    parents, known = {}, None
    if os.path.exists(db_path):
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                rows = con.execute("SELECT s.name, p.name FROM subjects s LEFT JOIN subjects p ON p.id = s.parent_id").fetchall()
            finally:
                con.close()
            parents = {n: p for n, p in rows}
            known = set(parents)
        except sqlite3.Error:
            pass  # no subjects table (or unreadable): cannot tell which subjects exist
    if known is not None:
        known |= {e["subject"] for e in _read_array(data_path) if isinstance(e, dict) and isinstance(e.get("subject"), str)}
    return privacy.resolve_visibility(fact.subject, fact.statement, fact.visibility,
                                      rules.with_context(parents=parents, known_subjects=known),
                                      extra_text=(fact.notes, fact.trust_rationale, fact.recheck_rationale,
                                                  fact.source_quote, fact.source_locator))


def append_fact(fact, data_path=None, db_path=None):
    """Validate and append one fact. Returns an AddResult; never prints or exits. The stored
    visibility is the most restrictive of the request, the subject tag and the keyword list (#31)."""
    data_path = DATA_PATH if data_path is None else data_path
    db_path = DB_PATH if db_path is None else db_path
    errors, notes = validate_fact(fact, db_path)
    if errors:
        return AddResult(ok=False, errors=errors, notes=notes)
    try:
        resolution = resolve_privacy(fact, data_path, db_path)
    except (privacy.PrivacyRulesError, DataFileError) as e:
        return AddResult(ok=False, errors=[str(e)], notes=notes)
    entry = build_entry(fact, visibility=resolution.visibility)
    try:
        total = append_record(data_path, entry)
    except DataFileError as e:
        return AddResult(ok=False, errors=[str(e)], notes=notes)
    except OSError as e:
        return AddResult(ok=False, errors=[f"could not write {data_path}: {e}"], notes=notes)
    return AddResult(ok=True, notes=notes, entry=entry, total=total, privacy=resolution)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    fact = NewFact(
        statement=args.statement, subject=args.subject, trust_level=args.trust_level, no_decay=args.no_decay, domain=args.domain,
        is_original_claim=args.is_original_claim, is_personal=args.is_personal, visibility=args.visibility, kind=args.kind, valid_from=args.valid_from, valid_to=args.valid_to,
        trust_rationale=args.trust_rationale, notes=args.notes, recheck_by=args.recheck_by,
        recheck_rationale=args.recheck_rationale, source_citekey=args.source_citekey,
        source_locator=args.source_locator, source_quote=args.source_quote, origin_path=args.origin_path,
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
    if result.privacy is not None and result.privacy.raised_above_request:
        print(f"note: stored as private although {fact.visibility} was requested -- {result.privacy.explain()}", file=sys.stderr)
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
