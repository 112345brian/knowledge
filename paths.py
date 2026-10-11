"""Loads path values from a private data checkout instead of hardcoding
machine-specific paths here. The private values and fact/source data live
outside this repository.

Nothing is read when this module is imported. `settings()` loads the private checkout's `local_paths.py` on
first use and returns one frozen `Settings`; `configure(settings)` replaces it (a test, an embedding
program), `configure(None)` forgets it. The old module-level names (`paths.PRIVATE_DATA_DIR`, ...) still
resolve, lazily, through the same object, so scripts that read them at their own start-up keep working.

If the private data checkout isn't available, the first use fails with a clear ModuleNotFoundError.

Set KNOWLEDGE_PRIVATE_DIR to point at the checkout from anywhere else, e.g. a
`.claude/worktrees/*` git worktree where the sibling path doesn't exist. When
it is set it wins, and a bad value is an error, never a silent fallback.
"""
import os
import sys
from dataclasses import dataclass
from typing import Optional, Tuple

_ENV_VAR = "KNOWLEDGE_PRIVATE_DIR"
_SIBLING_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "knowledge-private")
)


@dataclass(frozen=True)
class Settings:
    """Where this person's data lives. Optional client sources (see build_rules.CLIENT_SOURCES): which of them
    this checkout builds and the input files they read; a checkout that does not use a source leaves its
    paths None and the build never looks for them."""
    knowledge_db_dir: str
    private_data_dir: str
    bodybuilding_vault: str
    health_dir: str
    client_sources: Tuple[str, ...] = ()
    client_sources_implicit: bool = False
    concerts_csv: Optional[str] = None
    rym_export_csv: Optional[str] = None
    scrobbles_json: Optional[str] = None

    @property
    def db_dir(self):
        return os.path.expanduser(self.knowledge_db_dir)

    @property
    def db_path(self):
        return os.path.join(self.db_dir, "knowledge.db")

    @property
    def facts_path(self):
        return os.path.join(self.private_data_dir, "general_facts.json")


def load():
    """Read the private checkout's local_paths.py (no caching; `settings()` caches)."""
    override = os.environ.get(_ENV_VAR)
    private_dir = os.path.abspath(os.path.expanduser(override)) if override else _SIBLING_DIR
    if not os.path.isfile(os.path.join(private_dir, "local_paths.py")):
        source = f"{_ENV_VAR}={override!r}" if override else "the sibling default"
        raise ModuleNotFoundError(
            f"local_paths.py not found in {private_dir} (from {source}). Set {_ENV_VAR} to "
            f"your knowledge-private checkout, or check it out at {_SIBLING_DIR}.",
            name="local_paths",
        )
    if private_dir not in sys.path:
        sys.path.insert(0, private_dir)
    import local_paths as local
    declared = getattr(local, "CLIENT_SOURCES", None)
    # A local_paths.py written before CLIENT_SOURCES existed has no such name but does define the music inputs:
    # it built everything then, so it keeps doing so (build.py says it is guessing). A config that names the
    # sources, or defines no music inputs, is taken at its word.
    implicit = declared is None and bool(getattr(local, "CONCERTS_CSV", None))
    sources = ("concerts", "ratings", "scrobbles", "measurements", "claims") if implicit else tuple(declared or ())
    return Settings(
        knowledge_db_dir=local.KNOWLEDGE_DB_DIR, private_data_dir=local.PRIVATE_DATA_DIR,
        bodybuilding_vault=local.BODYBUILDING_VAULT, health_dir=local.HEALTH_DIR,
        client_sources=sources, client_sources_implicit=implicit,
        concerts_csv=getattr(local, "CONCERTS_CSV", None), rym_export_csv=getattr(local, "RYM_EXPORT_CSV", None),
        scrobbles_json=getattr(local, "SCROBBLES_JSON", None),
    )


_current: Optional[Settings] = None


def settings():
    global _current
    if _current is None:
        _current = load()
    return _current


def configure(new: Optional[Settings]):
    """Use `new` instead of reading local_paths.py; None forgets the cached value so the next use reloads."""
    global _current
    _current = new


class Defaults:
    """The `FactDefaults` port: where the facts file and the db are, resolved from `settings()` on every call."""
    data_path = property(lambda self: settings().facts_path)
    db_path = property(lambda self: settings().db_path)


_LEGACY = {
    "KNOWLEDGE_DB_DIR": "knowledge_db_dir", "PRIVATE_DATA_DIR": "private_data_dir",
    "BODYBUILDING_VAULT": "bodybuilding_vault", "HEALTH_DIR": "health_dir",
    "CLIENT_SOURCES": "client_sources", "CLIENT_SOURCES_IMPLICIT": "client_sources_implicit",
    "CONCERTS_CSV": "concerts_csv", "RYM_EXPORT_CSV": "rym_export_csv", "SCROBBLES_JSON": "scrobbles_json",
}


def __getattr__(name):
    if name in _LEGACY:
        return getattr(settings(), _LEGACY[name])
    raise AttributeError(f"module 'paths' has no attribute {name!r}")
