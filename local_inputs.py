"""Adapter for `inputs_port.Inputs`: the real file system, the vault's sqlite database, and the file-fingerprint
and acquisition-attribute adapters. Stateless; the module is the adapter."""
import glob as _glob
import json
import os
import sqlite3

import acquisition_store
import fixity_store
import locations
import locks


def exists(path):
    return os.path.exists(path)


def read_text(path, encoding="utf-8"):
    with locks.open_retrying(path, encoding=encoding) as f:
        return f.read()


def read_json(path):
    with locks.open_retrying(path, encoding="utf-8") as f:
        return json.load(f)


def list_files(directory, pattern):
    """Paths in `directory` matching the glob `pattern`, sorted."""
    return sorted(locations.canonical_path(p) for p in _glob.glob(os.path.join(directory, pattern)))


def open_database(path):
    """A connection to the sqlite file at `path` (the vault's own database; the build only reads it)."""
    return sqlite3.connect(path)


def fingerprint(path):
    return fixity_store.fingerprint(path)


def acquisition(row, path):
    return acquisition_store.resolve(row, path)
