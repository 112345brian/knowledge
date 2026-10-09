"""Loads actual path values from the private companion repo instead of
hardcoding any personal path here. This file (the public `knowledge` repo)
carries zero personal path strings on purpose -- the real values, and the
personal fact/source data itself, live only in `knowledge-private`, a
sibling checkout at ../knowledge-private (relative to this repo).

Every script that needs a path imports it from here, never inline:
    from paths import BODYBUILDING_VAULT as VAULT

If `knowledge-private` isn't checked out next to this repo, this import
fails with a clear ModuleNotFoundError -- that's expected: this public repo
isn't meant to run standalone without its private companion.

Set KNOWLEDGE_PRIVATE_DIR to point at the checkout from anywhere else, e.g. a
`.claude/worktrees/*` git worktree where the sibling path doesn't exist. When
it is set it wins, and a bad value is an error, never a silent fallback.
"""
import os
import sys

_ENV_VAR = "KNOWLEDGE_PRIVATE_DIR"
_SIBLING_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "knowledge-private")
)
_override = os.environ.get(_ENV_VAR)
_PRIVATE_DIR = os.path.abspath(os.path.expanduser(_override)) if _override else _SIBLING_DIR

if not os.path.isfile(os.path.join(_PRIVATE_DIR, "local_paths.py")):
    _source = f"{_ENV_VAR}={_override!r}" if _override else "the sibling default"
    raise ModuleNotFoundError(
        f"local_paths.py not found in {_PRIVATE_DIR} (from {_source}). Set {_ENV_VAR} to "
        f"your knowledge-private checkout, or check it out at {_SIBLING_DIR}.",
        name="local_paths",
    )
if _PRIVATE_DIR not in sys.path:
    sys.path.insert(0, _PRIVATE_DIR)

import local_paths as _local  # noqa: E402
from local_paths import (  # noqa: E402
    KNOWLEDGE_DB_DIR,
    BODYBUILDING_VAULT,
    HEALTH_DIR,
    PRIVATE_DATA_DIR,
)

# Optional client sources (see build_rules.CLIENT_SOURCES): which of them this checkout builds, and the input
# files they read. A checkout that does not use a source defines neither its name nor its paths, and the build
# never looks for them.
CLIENT_SOURCES = tuple(getattr(_local, "CLIENT_SOURCES", ()))
CONCERTS_CSV = getattr(_local, "CONCERTS_CSV", None)
RYM_EXPORT_CSV = getattr(_local, "RYM_EXPORT_CSV", None)
SCROBBLES_JSON = getattr(_local, "SCROBBLES_JSON", None)
