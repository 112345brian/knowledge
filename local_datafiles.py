"""Adapter for `datafiles_port.DataFiles`: the real files, the revisions store's array reader and atomic writer,
and the inter-process lock the data-file writers share. Stateless; the module is the adapter."""
import locks
import revisions_store


def exists(path):
    import os
    return os.path.exists(path)


def read_text(path, encoding="utf-8", newline=None):
    with locks.open_retrying(path, encoding=encoding, newline=newline) as f:
        return f.read()


def write_text(path, text, encoding="utf-8", newline=None):
    with open(path, "w", encoding=encoding, newline=newline) as f:
        f.write(text)


def write_text_atomic(path, text):
    revisions_store.atomic_write_text(path, text)


def read_array(path):
    return revisions_store.read_array(path)


def load_entries(data_dir):
    return revisions_store.load_entries(data_dir)


def locked(path):
    """The inter-process lock for the data file at `path` (a context manager)."""
    return locks.file_lock(locks.lock_path(path))
