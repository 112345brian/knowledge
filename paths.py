"""Reads where this person's data lives from the private data checkout, once, at an entry point.

`load()` returns a `Locations` (see locations.py) and keeps nothing: no cache, no module-level names, no
override. Whoever starts the program (the Typer app's `Container`, `build.py`, a tool's `main()`, a test)
calls it and hands the result down. The private values and fact/source data live outside this repository.

If the private data checkout isn't available, `load()` fails with a clear ModuleNotFoundError.

Set KNOWLEDGE_PRIVATE_DIR to point at the checkout from anywhere else, e.g. a
`.claude/worktrees/*` git worktree where the sibling path doesn't exist. When
it is set it wins, and a bad value is an error, never a silent fallback.
"""
import importlib.util
import os

import locations
from locations import Locations

_ENV_VAR = "KNOWLEDGE_PRIVATE_DIR"
_SIBLING_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "knowledge-private")
)


def expand(path):
    """`path` with `~` resolved: under $KNOWLEDGE_HOME when that is set (a machine whose data lives outside the OS
    user's home, e.g. `~/MEGA` on another drive), else the OS user's home. Only the expanded form is affected."""
    home = os.environ.get(locations.HOME_ENV_VAR)
    return os.path.expanduser(locations.expand_home(path, home) if home else path)


def load():
    """Read the private checkout's local_paths.py and return the `Locations` it describes."""
    override = os.environ.get(_ENV_VAR)
    private_dir = os.path.abspath(expand(override)) if override else _SIBLING_DIR
    if not os.path.isfile(os.path.join(private_dir, "local_paths.py")):
        source = f"{_ENV_VAR}={override!r}" if override else "the sibling default"
        raise ModuleNotFoundError(
            f"local_paths.py not found in {private_dir} (from {source}). Set {_ENV_VAR} to "
            f"your knowledge-private checkout, or check it out at {_SIBLING_DIR}.",
            name="local_paths",
        )
    # Executed from its file each time: two checkouts in one process (tests, tools) never share a cached module.
    spec = importlib.util.spec_from_file_location("local_paths", os.path.join(private_dir, "local_paths.py"))
    local = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(local)
    declared = getattr(local, "CLIENT_SOURCES", None)
    # A local_paths.py written before CLIENT_SOURCES existed has no such name but does define the music inputs:
    # it built everything then, so it keeps doing so (build.py says it is guessing). A config that names the
    # sources, or defines no music inputs, is taken at its word.
    implicit = declared is None and bool(getattr(local, "CONCERTS_CSV", None))
    sources = ("concerts", "ratings", "scrobbles", "measurements", "claims") if implicit else tuple(declared or ())

    def path(name):
        value = getattr(local, name, None)
        return locations.canonical_path(expand(value)) if value else None

    def written(*names):
        """The first of `names` that is set, exactly as local_paths.py wrote it."""
        return next((getattr(local, n) for n in names if getattr(local, n, None)), None)

    # Every path is canonical (forward slashes) from here down, so whatever the build stores is the same on every
    # OS; Windows opens such paths as they are. `*_written` keeps local_paths.py's own text.
    return Locations(
        knowledge_db_dir=locations.canonical_path(expand(local.KNOWLEDGE_DB_DIR)),
        private_data_dir=locations.canonical_path(local.PRIVATE_DATA_DIR),
        private_dir=locations.canonical_path(private_dir),
        # NOTES_VAULT is the generic name; BODYBUILDING_VAULT is the older one and still works.
        notes_vault=path("NOTES_VAULT") or path("BODYBUILDING_VAULT"), health_dir=path("HEALTH_DIR"),
        client_sources=sources, client_sources_implicit=implicit,
        notes_vault_written=written("NOTES_VAULT", "BODYBUILDING_VAULT"), health_dir_written=written("HEALTH_DIR"),
        concerts_csv=path("CONCERTS_CSV"), rym_export_csv=path("RYM_EXPORT_CSV"), scrobbles_json=path("SCROBBLES_JSON"),
        concerts_csv_written=written("CONCERTS_CSV"), rym_export_csv_written=written("RYM_EXPORT_CSV"),
        scrobbles_json_written=written("SCROBBLES_JSON"),
    )
