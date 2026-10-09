"""Adapter for adding facts: the locked, atomically written JSON array file, plus the two db lookups
(a source citekey, the subject tree). The rules are in `new_fact`; the use case is `add_fact`.
"""
import contextlib
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import uuid

import fixity_store
import subjects_store
from new_fact import DataFileError


def lock_path(path):
    """Lock sidecar in the system temp dir, keyed by the data file's real path, so no lock file
    ever appears inside knowledge-private (it would break the clean-tree check in issue #10)."""
    key = hashlib.sha1(os.path.realpath(path).encode()).hexdigest()
    directory = os.path.join(tempfile.gettempdir(), "knowledge-locks")
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, key + ".lock")


@contextlib.contextmanager
def file_lock(lock_path):
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


def read_array(path):
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


def atomic_write_json(path, items):
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
    with file_lock(lock_path(path)):
        items = read_array(path)
        items.append(record)
        atomic_write_json(path, items)
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
    with file_lock(lock_path(path)):
        items = read_array(path)
        rejected, accepted = [], 0
        for index, record in enumerate(records):
            reason = reject(items, record) if reject is not None else None
            if reason:
                rejected.append((index, reason))
            else:
                items.append(record)
                accepted += 1
        if accepted:
            atomic_write_json(path, items)
    return len(items), rejected


def citekey_problem(db_path, citekey):
    """(error | None, note | None) for a source citekey checked against the db's `sources` table."""
    if not os.path.exists(db_path):
        return None, f"  (skipping citekey check -- {db_path} doesn't exist yet)"
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT 1 FROM sources WHERE citekey = ?", (citekey,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        return f"could not check --source-citekey against {db_path}: {e}", None
    if not row:
        return f"--source-citekey {citekey!r} not found in sources table", None
    return None, None


def subject_tree(db_path):
    """({subject: parent | None}, known subjects) from the db, or ({}, None) when there is no usable
    subjects table (nothing to compare against)."""
    if not os.path.exists(db_path):
        return {}, None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT s.name, p.name FROM subjects s LEFT JOIN subjects p ON p.id = s.parent_id").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}, None  # no subjects table (or unreadable): cannot tell which subjects exist
    parents = {n: p for n, p in rows}
    return parents, set(parents)


def file_subjects(data_path):
    """The subjects already named in the facts file."""
    return {e["subject"] for e in read_array(data_path) if isinstance(e, dict) and isinstance(e.get("subject"), str)}


def data_dir_of(data_path):
    """The directory a facts file lives in (relative paths resolve against the current directory)."""
    return os.path.dirname(os.path.abspath(data_path))


def file_sha256(path):
    """The SHA-256 of the file a fact was extracted from (#38), or None when it is unreadable."""
    return fixity_store.fingerprint(path)["content_sha256"] or None


def canonical_subject(data_path, subject):
    """(canonical subject, notes, error) for a new fact's subject against subjects.json beside the facts file
    (#43). An alias is replaced by its canonical name and noted; a deprecated subject is an error naming the
    replacement. With no subjects.json the subject is unchanged. Never raises."""
    path = os.path.join(data_dir_of(data_path), subjects_store.SUBJECTS_FILENAME)
    try:
        entries = subjects_store.read_file(path)
    except subjects_store.SubjectsError as e:
        return subject, [], str(e)
    if not entries:
        return subject, [], None
    canon, notes, error = subjects_store.check_new_fact(subject, entries)
    return (subject if error else canon), notes, error
