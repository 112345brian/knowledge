"""Adapter for `build_files_port.BuildFiles`: the real disk, sqlite files and module loader. Stateless; the module
is the adapter."""
import importlib.util
import os
import shutil
import sqlite3
import tempfile
import uuid


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def load_module(path):
    """Execute the Python file at `path` as a fresh module (a build step)."""
    spec = importlib.util.spec_from_file_location(os.path.basename(path), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def create_exclusive(path):
    """Create an empty file that must not exist yet. The kernel applies the umask; reading it with os.umask(0)
    would change the process-wide setting while other threads may be creating files."""
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
    os.close(fd)


def remove_if_exists(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def join(directory, name):
    return os.path.join(directory, name)


def makedirs(path):
    os.makedirs(path, exist_ok=True)


def exists(path):
    return os.path.exists(path)


def replace(source, target):
    os.replace(source, target)


def copy(source, target):
    shutil.copy2(source, target)


def listdir(path):
    return os.listdir(path)


def remove(path):
    os.remove(path)


def size(path):
    return os.path.getsize(path)


def open_database(path):
    return sqlite3.connect(path)


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def remove_tree(path):
    shutil.rmtree(path, ignore_errors=True)


def make_temp_dir(prefix):
    return tempfile.mkdtemp(prefix=prefix)


def system_temp_dir():
    return tempfile.gettempdir()


def temporary_directory():
    return tempfile.TemporaryDirectory()


def unique_token():
    return uuid.uuid4().hex
