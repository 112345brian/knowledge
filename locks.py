"""Adapter for the inter-process file lock: the sidecar lock file and the exclusive lock on it (fcntl on POSIX,
msvcrt on Windows). Shared by the writers of the data files in knowledge-private (the facts file, the revision
log, the one-time backfills) so that two processes never read-modify-write the same file at once.
"""
import contextlib
import hashlib
import os
import tempfile
import time


def lock_path(path):
    """Lock sidecar in the system temp dir, keyed by the data file's real path, so no lock file
    ever appears inside knowledge-private (it would break the clean-tree check in issue #10)."""
    key = hashlib.sha1(os.path.realpath(path).encode()).hexdigest()
    directory = os.path.join(tempfile.gettempdir(), "knowledge-locks")
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, key + ".lock")


# Windows refuses to replace a file another handle has open (a reader that took no lock), reporting it as
# PermissionError; the other handle is a momentary read, so a short bounded retry is what makes the swap succeed.
_REPLACE_RETRY_SECONDS = 5.0
_OPEN_RETRY_SECONDS = 2.0


def replace_file(source, target):
    """`os.replace(source, target)`, retrying briefly while a concurrent reader holds `target` open (Windows only)."""
    deadline = time.monotonic() + _REPLACE_RETRY_SECONDS
    delay = 0.005
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.1)


def open_retrying(path, mode="r", **kwargs):
    """`open(path, mode, **kwargs)`, retrying briefly on PermissionError (Windows only). A reader that takes no
    lock can open the file in the instant a writer's `replace_file` swaps it in, which Windows reports as
    "permission denied"; the swap is momentary, so the same open succeeds a few milliseconds later."""
    deadline = time.monotonic() + _OPEN_RETRY_SECONDS
    delay = 0.005
    while True:
        try:
            return open(path, mode, **kwargs)
        except PermissionError:
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.1)


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
