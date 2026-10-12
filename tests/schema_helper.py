"""The full schema text for tests that need the optional client tables too: schema.sql plus every client fragment."""
import client_bootstrap
import os

import build_rules

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def full_schema(sources=None):
    """schema.sql plus the fragments of `sources` (default: every client source)."""
    names = client_bootstrap.registry().schema_files(client_bootstrap.registry().names if sources is None else sources)
    return "\n".join(open(os.path.join(REPO, n)).read() for n in names)
