"""Adapter for the fact revision log (#30): the entry files, the log file and its lock, the clock and
the db. The rules (keys, freshness, record shape, the next revision) are in `revisions`, the domain;
this module reads and writes, and calls them.

API (the Typer `history` / `show --as-of` commands are thin callers):
    append_revision(source_key, changes, reason, via, session_id=None)  -> revisions.RevisionResult
    get_history(db, fact_id | source_key)                               -> [revision dict, ...]
    get_fact_as_of(db, fact_id | source_key, "YYYY-MM-DD")              -> revision dict | None

Nothing here reads paths.py at import time (defaults resolve lazily), so tests can point every
function at a temp directory.
"""
import contextlib
import json
import os
import sqlite3
import stat
import uuid

import fact_tables_store
import revision_apply_service
import revisions
import locks
from revisions import ENTRY_FILES, REVISIONS_FILENAME, REVISION_KEYS, RevisionError, RevisionResult


def read_array(path):
    try:
        with open(path) as f:
            items = json.load(f)
    except json.JSONDecodeError as e:
        raise RevisionError(f"{path} is not valid JSON ({e})") from e
    return revisions.check_array(items, path)


def load_entries(data_dir):
    """Every fact entry in the data files, in file order, as dicts
    {file, index, key, entry, legacy_date}. Missing files are skipped. A duplicate key fails."""
    files = []
    for name, legacy_date in ENTRY_FILES:
        path = os.path.join(data_dir, name)
        if os.path.exists(path):
            files.append((name, legacy_date, read_array(path)))
    return revisions.collect_entries(files)


def insert_revision_row(cur, fact_id, rev):
    fact_tables_store.insert_revision(cur, fact_id, rev)


def entry_base(entries):
    """{source_key: the original state of its inherited-by-legacy-lines fields} for `entries` (load_entries)."""
    out = {}
    for e in entries:
        snap = revisions.implicit_revision(e["key"], e["entry"], e["legacy_date"], e["file"])
        out[e["key"]] = {f: snap[f] for f in revisions.LEGACY_INHERITED}
    return out


def read_log(path, base=None):
    """Parse the log: [(line_number, record)]. Blank lines are skipped. A line that is not a
    valid record raises RevisionError naming `path:line`. Missing file == empty log. `base` is
    {source_key: the fact's original state} and supplies the fields a pre-#39 line lacks (see
    revisions.parse_log); callers that hold the entries or the db pass it."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return revisions.parse_log(f, path, base)


def atomic_write_text(path, text):
    """Write `text` to `path` via a temp file + os.replace (a crash leaves the old or the new
    file, never half). Keeps the file's mode; a new file gets the umask default."""
    # mode 0o666 at creation: the kernel applies the umask (os.umask(0) would change it process-wide)
    tmp = f"{os.path.abspath(path)}.{uuid.uuid4().hex}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def append_revision(source_key, changes, reason, via, session_id=None, data_dir=None, revisions_path=None, expect=None, *, clock):
    """Append one revision to a fact: locked, validated, atomic. Never raises for bad input and
    never touches the original entry files; returns RevisionResult(ok, errors, revision).

    `changes` maps mutable field names to new values; unchanged fields are carried over from
    the fact's current state, so the stored line is always a full snapshot. A call that
    changes nothing is refused (it would only add noise to the history).

    `expect` (optional dict of mutable field -> value) is a precondition checked under the same
    lock as the write: if the fact's current state differs, nothing is written and the result is
    not ok with error "precondition failed: ...". review.approve uses it so a fact that was
    retracted by another process between the check and the write is never silently re-activated."""
    if data_dir is None:
        raise ValueError("append_revision needs the data dir the entry files and the revision log live in")
    revisions_path = os.path.join(data_dir, REVISIONS_FILENAME) if revisions_path is None else revisions_path
    changes, bad = revisions.normalize_changes(changes)
    if bad:
        return RevisionResult(False, [bad])
    errors = revisions.check_request(changes, reason, via, session_id, expect)
    if errors:
        return RevisionResult(False, errors)
    try:
        with locks.file_lock(locks.lock_path(revisions_path)):
            entries = load_entries(data_dir)
            records = read_log(revisions_path, entry_base(entries))
            result = revisions.plan_revision(source_key, changes, reason, via, session_id, expect,
                                             entries, records, clock.now_iso(), revisions_path)
            if not result.ok:
                return result
            existing = ""
            if os.path.exists(revisions_path):
                with open(revisions_path, encoding="utf-8", newline="") as f:
                    existing = f.read()
            if existing and not existing.endswith("\n"):
                existing += "\n"
            atomic_write_text(revisions_path, existing + revisions.log_line(result.revision) + "\n")
    except RevisionError as e:
        return RevisionResult(False, [str(e)])
    except OSError as e:
        return RevisionResult(False, [f"could not write {revisions_path}: {e}"])
    return result


def apply_revisions(con, revisions_path):
    """Validates the log against the facts and revision-1 rows already in `con`, inserts revisions >= 2 and
    writes each touched fact's latest revision into `facts` (the use case is
    revision_apply_service.apply_revision_log; this binds it to SQLite and the log file). Raises
    RevisionError (naming path:line) on any violation. Commits. Returns the number of revisions applied."""
    deps = revision_apply_service.RevisionLogDeps(revision_log=_LogFile(), revision_table=fact_tables_store.SqliteFactRevisionTable(con))
    return revision_apply_service.apply_revision_log(deps, revisions_path)


class _LogFile:
    """The log-reading port, over `read_log` (a file)."""

    def read_log(self, path, base=None):
        return read_log(path, base)


@contextlib.contextmanager
def connection(db):
    if isinstance(db, (str, os.PathLike)):
        con = sqlite3.connect(f"file:{os.fspath(db)}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            yield con
        finally:
            con.close()
    else:
        yield db


def revision_rows(con, ref):
    if isinstance(ref, bool) or not isinstance(ref, (int, str)):
        raise TypeError("ref must be a fact id (int) or a source_key (str)")
    column = "f.id" if isinstance(ref, int) else "f.source_key"
    cur = con.execute(
        f"""SELECT f.id AS fact_id, sub.name AS subject, r.*
            FROM fact_revisions r JOIN facts f ON f.id = r.fact_id JOIN subjects sub ON sub.id = f.subject_id
            WHERE {column} = ? ORDER BY r.revision""", (ref,))
    names = [d[0] for d in cur.description]
    rows = [dict(zip(names, r)) for r in cur.fetchall()]
    for r in rows:
        r.pop("id", None)
    return rows


def get_history(db, ref):
    """Every revision of a fact in order (revision 1 = the original entry), each a dict with
    fact_id, subject, source_key, revision, changed_at, changed_via, session_id, change_reason
    and the mutable fields. `db` is a sqlite connection (or a path, opened read-only);
    `ref` is a fact id (int) or a source_key (str). Unknown fact -> []."""
    with connection(db) as con:
        return revision_rows(con, ref)


def get_fact_as_of(db, ref, as_of):
    """The fact as it stood at `as_of`: the latest revision whose changed_at is <= that moment,
    or None if the fact did not exist yet (or does not exist). Raises ValueError on a bad date."""
    revisions.parse_as_of(as_of)  # a bad date raises before any query
    with connection(db) as con:
        return revisions.revision_as_of(revision_rows(con, ref), as_of)
