"""File fixity and identity for source files (#38). Library only; `knowledge.py audit-sources` wraps it.

Archival practice records fixity (a checksum, a size, a format) so an object's identity does not depend
on where it lives and a change is detectable. `fingerprint(path)` is that record for one file;
`audit_sources(db)` compares the baseline a fact was extracted from (facts.extracted_from_sha256) with
what is on disk now.

A file that is missing or unreadable gets file_state = 'missing' and NULL hash/size/mtime: a hash is
never invented and nothing here raises for a bad path. Caveat: ANY edit to a note, including a trivial
frontmatter edit, changes its hash and raises an audit row. Hashing only the note body is the follow-up
if that proves noisy.

Paths are stored as written (they may begin with `~`); every read goes through expanduser.
"""
import hashlib
import mimetypes
import os
import re
from datetime import datetime, timezone

HASH_BUFFER = 1 << 20  # read files in 1 MiB chunks, never whole
FILE_STATES = ("present", "missing")  # keep in sync with the CHECKs in schema.sql
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REASONS = ("changed", "missing", "moved")

# The stdlib table varies by platform/version; the vault is markdown, so pin the types we rely on.
_MIME_OVERRIDES = {".md": "text/markdown", ".markdown": "text/markdown", ".json": "application/json",
                   ".csv": "text/csv", ".txt": "text/plain", ".pdf": "application/pdf"}


def valid_sha256(value):
    return isinstance(value, str) and bool(SHA256_RE.match(value))


def _expand(path):
    return os.path.expanduser(path)


def sha256_of(path):
    """Hex SHA-256 of the file at `path`, read in chunks. Raises OSError if it cannot be read."""
    h = hashlib.sha256()
    with open(_expand(path), "rb") as f:
        for chunk in iter(lambda: f.read(HASH_BUFFER), b""):
            h.update(chunk)
    return h.hexdigest()


def guess_mime(path):
    ext = os.path.splitext(path)[1].lower()
    return _MIME_OVERRIDES.get(ext) or mimetypes.guess_type(path)[0]


def fingerprint(path):
    """{content_sha256, size_bytes, file_mtime, mime_type, file_state} for one file.
    file_state is 'present' or 'missing' (absent, a directory, unreadable); a missing file has None
    for the hash, size and mtime. The mtime is UTC ISO-8601 to the second."""
    missing = {"content_sha256": None, "size_bytes": None, "file_mtime": None,
               "mime_type": guess_mime(path) if path else None, "file_state": "missing"}
    if not path:
        return missing
    real = _expand(path)
    try:
        digest = sha256_of(real)
        st = os.stat(real)
    except OSError:  # includes IsADirectoryError, PermissionError, FileNotFoundError
        return missing
    mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).replace(microsecond=0).isoformat()
    return {"content_sha256": digest, "size_bytes": st.st_size, "file_mtime": mtime,
            "mime_type": guess_mime(path), "file_state": "present"}


FINGERPRINT_COLUMNS = ("content_sha256", "size_bytes", "file_mtime", "mime_type", "file_state")


def _index_roots(search_roots, wanted_exts):
    """{sha256: path} for every file under the roots with one of the wanted extensions."""
    found = {}
    for root in search_roots:
        for dirpath, _dirs, files in os.walk(_expand(root)):
            for name in sorted(files):
                if os.path.splitext(name)[1].lower() not in wanted_exts:
                    continue
                full = os.path.join(dirpath, name)
                try:
                    found.setdefault(sha256_of(full), full)
                except OSError:
                    continue
    return found


def _baseline_rows(db):
    return db.execute(
        """SELECT f.id AS fact_id, f.source_key, f.statement, vf.path, f.extracted_from_sha256
           FROM facts f JOIN vault_files vf ON vf.id = f.origin_file_id
           WHERE f.extracted_from_sha256 IS NOT NULL
           ORDER BY f.id""").fetchall()


def audit_sources(db, search_roots=()):
    """Facts whose source file no longer matches the hash it had when the fact was extracted.
    One dict per fact, ordered by fact_id: fact_id, source_key, statement, path, reason, baseline_sha256,
    current_sha256, moved_to.
      changed  the file is there but its hash differs from the baseline
      missing  the file is gone and no file with the baseline hash was found
      moved    the file is gone but a file with the baseline hash exists elsewhere (`moved_to`):
               among the other vault_files paths, or under any of `search_roots`
    Facts with no baseline are NOT rows here (see unbaselined_facts). Never writes. `db` is an open
    sqlite3 connection; hashes are computed from the files on disk now, not read from the db."""
    rows = _baseline_rows(db)
    cache = {}

    def current(path):
        if path not in cache:
            cache[path] = fingerprint(path)["content_sha256"]
        return cache[path]

    out, gone = [], []
    for fact_id, key, statement, path, baseline in rows:
        now = current(path)
        if now == baseline:
            continue
        rec = {"fact_id": fact_id, "source_key": key, "statement": statement, "path": path,
               "reason": "changed" if now is not None else "missing",
               "baseline_sha256": baseline, "current_sha256": now, "moved_to": None}
        out.append(rec)
        if now is None:
            gone.append(rec)
    if gone:
        by_hash = {}
        for (p,) in db.execute("SELECT path FROM vault_files ORDER BY id"):
            h = current(p)
            if h is not None:
                by_hash.setdefault(h, p)
        if search_roots:
            exts = {os.path.splitext(r["path"])[1].lower() for r in gone}
            for h, p in _index_roots(search_roots, exts).items():
                by_hash.setdefault(h, p)
        for rec in gone:
            target = by_hash.get(rec["baseline_sha256"])
            if target is not None:
                rec["reason"], rec["moved_to"] = "moved", target
    return out


def unbaselined_facts(db):
    """Facts that cite a source file but have no recorded baseline hash, so the audit cannot judge
    them: [{fact_id, source_key, path}]. Reported separately, never as a change."""
    return [{"fact_id": i, "source_key": k, "path": p} for i, k, p in db.execute(
        """SELECT f.id, f.source_key, vf.path FROM facts f JOIN vault_files vf ON vf.id = f.origin_file_id
           WHERE f.extracted_from_sha256 IS NULL ORDER BY f.id""")]
