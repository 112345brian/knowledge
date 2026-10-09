"""Adapter for the inter-process file lock: the sidecar lock file and the exclusive lock on it (fcntl on POSIX,
msvcrt on Windows). Shared by the writers of the data files in knowledge-private (the facts file, the revision
log, the one-time backfills) so that two processes never read-modify-write the same file at once.
"""
import contextlib
import hashlib
import os
import tempfile


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
