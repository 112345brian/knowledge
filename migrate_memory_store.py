"""Adapter for migrate-memory (#29): finds and reads the memory files, holds the run lock, and lists the
source keys already in the entry files. The rules are in `migrate_memory_rules`; the use case is
`migrate_memory`.
"""
import contextlib
import glob
import hashlib
import os
import tempfile

import revisions
import revisions_store
from migrate_memory_rules import MAX_FILE_BYTES, MemoryFile, parse_memory_bytes


def absolute_root(root):
    """The memory root as an absolute path (~ expanded)."""
    return os.path.abspath(os.path.expanduser(root))


def root_exists(root):
    return os.path.isdir(root)


def data_dir_of(data_path):
    """The directory a facts file lives in."""
    return os.path.dirname(os.path.abspath(data_path))


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
    return parse_memory_bytes(raw, mf)


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


def lock_for(data_path):
    key = hashlib.sha1(os.path.realpath(data_path).encode()).hexdigest()
    directory = os.path.join(tempfile.gettempdir(), "knowledge-locks")
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, "migrate-memory-" + key + ".lock")


@contextlib.contextmanager
def run_lock(data_path):
    f = open(lock_for(data_path), "a+")
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
        for item in revisions_store.read_array(path):
            key = item.get("source_key")
            if isinstance(key, str):
                out[key] = item.get("notes") if isinstance(item.get("notes"), str) else ""
    return out


