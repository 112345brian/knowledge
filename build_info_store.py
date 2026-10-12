"""Build info and an inputs manifest (#47). Library only; `knowledge.py build-info` wraps the reads.

A knowledge.db cannot otherwise say which inputs and code produced it. Every build records, inside the db it
builds:
  build_info    one row: built_at, schema_version, code_commit/code_dirty (this repo), private_commit/
                private_dirty (knowledge-private), python_version, sqlite_version, platform
  build_inputs  one row per input, by a STABLE KEY (never a path): state (present | missing), sha256,
                size_bytes, file_mtime, read_at

Schema version: the single constant is `PRAGMA user_version = N;` in schema.sql. Bump it whenever a table,
column, constraint, view or trigger changes (tests/test_build_info.py pins a fingerprint of schema.sql and
fails with the instruction when you forget). Comment and whitespace edits do not count.

An input that does not exist is recorded as state `missing` (hash, size and mtime NULL), never an error:
several inputs are optional. `read_at` is when the build looked; everything else is deterministic, so two
builds over identical inputs give identical (key, state, sha256, size_bytes, file_mtime) rows.

Normal-only DB: built_at and schema_version only (normal_rules.py, normal_db_store.py); commits, versions and keys stay out.
"""
import hashlib
import os
import platform
import sys
import sqlite3

import fixity_store
from build_port import InputState, Provenance, RepoState
from private_git_port import PrivateGit
from locations import join_path

KEY_PREFIX = "input:"
# (stable key, how to find it). Data files live in the private data dir; the others come from the locations.
DATA_FILES = ("manual_sources.json", "pilot_facts.json", "facts_batch1.json", "facts_batch2.json", "facts_batch3.json",
              "facts_batch4.json", "general_facts.json", "fact_revisions.jsonl", "privacy_rules.json", "entities.json",
              "subjects.json", "fact_hints.json")
NOTES_KEY = KEY_PREFIX + "vault-source-notes"


def input_paths(locations, clients=None):
    """[(stable key, path)] for every file input the build can read, for these `locations`: the core's, and the
    inputs and private data files the registered client sources declare (`clients`, a `ClientRegistry`)."""
    out = []
    data_files = list(DATA_FILES)
    if clients is not None:
        # A client input exists only for a checkout that has it configured (the others are None).
        core_dirs = {"NOTES_VAULT": locations.notes_vault, "HEALTH_DIR": locations.health_dir}
        for i in clients.inputs():
            base = core_dirs[i.setting] if i.setting in core_dirs else locations.client_input(i.setting)
            if base:
                out.append((KEY_PREFIX + i.key, join_path(base, i.file) if i.file else base))
        data_files += [f for f in clients.data_files() if f not in data_files]
    out += [(KEY_PREFIX + "data/" + name, join_path(locations.private_data_dir, name)) for name in data_files]
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


def collect_inputs(con, locations, clients=None):
    """[(key, {state, sha256, size_bytes, file_mtime})] sorted by key."""
    out = []
    for key, path in input_paths(locations, clients):
        fp = fixity_store.fingerprint(path)
        out.append((key, {"state": fp["file_state"], "sha256": fp["content_sha256"],
                          "size_bytes": fp["size_bytes"], "file_mtime": fp["file_mtime"]}))
    out.append((NOTES_KEY, _notes_manifest(con)))
    return sorted(out, key=lambda kv: kv[0])


class BuildProvenance:
    """Adapter for `build_port.ProvenanceReader`: reads which inputs and code produced a freshly built db and writes
    that into its build_info / build_inputs tables. `code_dir` is the checkout the pipeline runs from."""

    def __init__(self, locations, code_dir, git: PrivateGit, clients=None):
        self._locations, self._code_dir, self._git, self._clients = locations, code_dir, git, clients

    def read(self, con) -> Provenance:
        code = self._git.describe_repo(self._code_dir)
        private = self._git.describe_repo(self._locations.private_data_dir)
        inputs = tuple(InputState(key, d["state"], d["sha256"], d["size_bytes"], d["file_mtime"])
                       for key, d in collect_inputs(con, self._locations, self._clients))
        return Provenance(schema_version=con.execute("PRAGMA user_version").fetchone()[0],
                          code=RepoState(code["commit"], code["dirty"]),
                          private=RepoState(private["commit"], private["dirty"]),
                          python_version=platform.python_version(), sqlite_version=sqlite3.sqlite_version, inputs=inputs,
                          platform=sys.platform)

    def write(self, con, provenance: Provenance, built_at: str, read_at: str) -> int:
        """Insert the build_info row and the build_inputs manifest into `con`. Does not commit; returns the id."""
        cur = con.execute(
            "INSERT INTO build_info (built_at, schema_version, code_commit, code_dirty, private_commit, private_dirty, "
            "python_version, sqlite_version, platform) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (built_at, provenance.schema_version, provenance.code.commit, provenance.code.dirty,
             provenance.private.commit, provenance.private.dirty, provenance.python_version, provenance.sqlite_version, provenance.platform))
        info_id = cur.lastrowid
        for i in provenance.inputs:
            con.execute("INSERT INTO build_inputs (build_id, input_key, state, sha256, size_bytes, file_mtime, read_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)", (info_id, i.key, i.state, i.sha256, i.size_bytes, i.file_mtime, read_at))
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
