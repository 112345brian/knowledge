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
"""
import os
import sys

_PRIVATE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "knowledge-private")
)
if _PRIVATE_DIR not in sys.path:
    sys.path.insert(0, _PRIVATE_DIR)

from local_paths import (  # noqa: E402
    KNOWLEDGE_DB_DIR,
    BODYBUILDING_VAULT,
    HEALTH_DIR,
    CONCERTS_CSV,
    RYM_EXPORT_CSV,
    SCROBBLES_JSON,
    PRIVATE_DATA_DIR,
)
