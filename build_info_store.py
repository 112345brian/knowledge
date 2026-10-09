"""Build info and an inputs manifest (#47). Library only; `knowledge.py build-info` wraps the reads.

A knowledge.db cannot otherwise say which inputs and code produced it. Every build records, inside the db it
builds:
  build_info    one row: built_at, schema_version, code_commit/code_dirty (this repo), private_commit/
                private_dirty (knowledge-private), python_version, sqlite_version
  build_inputs  one row per input, by a STABLE KEY (never a path): state (present | missing), sha256,
                size_bytes, file_mtime, read_at

Schema version: the single constant is `PRAGMA user_version = N;` in schema.sql. Bump it whenever a table,
column, constraint, view or trigger changes (tests/test_build_info.py pins a fingerprint of schema.sql and
fails with the instruction when you forget). Comment and whitespace edits do not count.

An input that does not exist is recorded as state `missing` (hash, size and mtime NULL), never an error:
several inputs are optional. `read_at` is when the build looked; everything else is deterministic, so two
builds over identical inputs give identical (key, state, sha256, size_bytes, file_mtime) rows.

Normal-only DB: built_at and schema_version only (normal_db.py); commits, versions and keys stay out.
"""
import hashlib
import os
import platform
import sqlite3

import clock
import fixity_store
import private_git

KEY_PREFIX = "input:"
# (stable key, how to find it). Data files live in the private data dir; the others come from paths.py.
DATA_FILES = ("manual_sources.json", "pilot_facts.json", "facts_batch1.json", "facts_batch2.json", "facts_batch3.json",
              "facts_batch4.json", "general_facts.json", "fact_revisions.jsonl", "privacy_rules.json", "entities.json",
              "subjects.json", "measurements_snapshot.json")
NOTES_KEY = KEY_PREFIX + "vault-source-notes"


def input_paths(paths_module):
    """[(stable key, path)] for every file input the build can read. `paths_module` is paths.py (or a stand-in)."""
    out = [(KEY_PREFIX + "vault-db", os.path.join(paths_module.BODYBUILDING_VAULT, "bodybuilding.db"))]
    # The music inputs only exist for a checkout that has them configured (paths.py leaves the others None).
    out += [(KEY_PREFIX + key, path) for key, path in (("concerts-csv", paths_module.CONCERTS_CSV),
                                                         ("rym-export-csv", paths_module.RYM_EXPORT_CSV),
                                                         ("scrobbles-json", paths_module.SCROBBLES_JSON)) if path]
    out += [(KEY_PREFIX + "data/" + name, os.path.join(paths_module.PRIVATE_DATA_DIR, name)) for name in DATA_FILES]
    return out


def _notes_manifest(con):
    """One aggregate input for the vault's source notes, from the hashes step 02 already stored (no re-reading):
    sha256 over the sorted 'name:sha' lines, total size, newest mtime. 'missing' when there are none."""
    try:
        rows = con.execute("SELECT origin_path, content_sha256, size_bytes, file_mtime FROM sources "
                           "WHERE content_sha256 IS NOT NULL ORDER BY origin_path").fetchall()
    except sqlite3.Error:
        rows = []
    if not rows:
        return {"state": "missing", "sha256": None, "size_bytes": None, "file_mtime": None}
    h = hashlib.sha256("\n".join(f"{os.path.basename(p)}:{s}" for p, s, _, _ in rows).encode()).hexdigest()
    return {"state": "present", "sha256": h, "size_bytes": sum(r[2] or 0 for r in rows),
            "file_mtime": max((r[3] for r in rows if r[3]), default=None)}


def collect_inputs(con, paths_module):
    """[(key, {state, sha256, size_bytes, file_mtime})] sorted by key."""
    out = []
    for key, path in input_paths(paths_module):
        fp = fixity_store.fingerprint(path)
        out.append((key, {"state": fp["file_state"], "sha256": fp["content_sha256"],
                          "size_bytes": fp["size_bytes"], "file_mtime": fp["file_mtime"]}))
    out.append((NOTES_KEY, _notes_manifest(con)))
    return sorted(out, key=lambda kv: kv[0])


def record(con, paths_module, code_dir, private_dir):
    """Write the build_info row and the build_inputs manifest into `con` (a freshly built db). Does not commit.
    Returns the build_info id."""
    code = private_git.describe_repo(code_dir)
    private = private_git.describe_repo(private_dir)
    version = con.execute("PRAGMA user_version").fetchone()[0]
    cur = con.execute(
        "INSERT INTO build_info (built_at, schema_version, code_commit, code_dirty, private_commit, private_dirty, "
        "python_version, sqlite_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (clock.now_iso(), version, code["commit"], code["dirty"], private["commit"], private["dirty"],
         platform.python_version(), sqlite3.sqlite_version))
    info_id, read_at = cur.lastrowid, clock.now_iso()
    for key, d in collect_inputs(con, paths_module):
        con.execute("INSERT INTO build_inputs (build_id, input_key, state, sha256, size_bytes, file_mtime, read_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)", (info_id, key, d["state"], d["sha256"], d["size_bytes"], d["file_mtime"], read_at))
    return info_id


def latest(db):
    """{'build': {...}, 'inputs': [{...}]} for the newest build in `db`, or None when it recorded none.
    Raises sqlite3.OperationalError for a db that predates build info."""
    old, db.row_factory = db.row_factory, sqlite3.Row
    try:
        info = db.execute("SELECT * FROM build_info ORDER BY id DESC LIMIT 1").fetchone()
        if info is None:
            return None
        inputs = [dict(r) for r in db.execute("SELECT input_key, state, sha256, size_bytes, file_mtime, read_at FROM build_inputs "
                                              "WHERE build_id = ? ORDER BY input_key", (info["id"],))]
        return {"build": dict(info), "inputs": inputs}
    finally:
        db.row_factory = old


def compare(a, b):
    """Inputs that differ between two `latest()` results: [{key, change, a_sha256, b_sha256}] sorted by key,
    change one of 'changed' (hash differs), 'now-missing', 'now-present', 'added', 'removed'. Unchanged inputs are omitted."""
    ia = {i["input_key"]: i for i in (a or {}).get("inputs", [])}
    ib = {i["input_key"]: i for i in (b or {}).get("inputs", [])}
    out = []
    for key in sorted(set(ia) | set(ib)):
        x, y = ia.get(key), ib.get(key)
        if x is None:
            change = "added"
        elif y is None:
            change = "removed"
        elif x["state"] != y["state"]:
            change = "now-missing" if y["state"] == "missing" else "now-present"
        elif x["sha256"] != y["sha256"]:
            change = "changed"
        else:
            continue
        out.append({"key": key, "change": change, "a_sha256": x and x["sha256"], "b_sha256": y and y["sha256"]})
    return out
