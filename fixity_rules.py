"""What a file fingerprint may hold: the vocabulary of a fixity record, free of how a file is read or hashed.

Model ring: imports nothing else in the repo and does no I/O. `fixity_store` (the adapter that hashes files and
audits the db) and the build steps share these names.
"""
import re

FILE_STATES = ("present", "missing")  # keep in sync with the CHECKs in schema.sql
FINGERPRINT_COLUMNS = ("content_sha256", "size_bytes", "file_mtime", "mime_type", "file_state")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def valid_sha256(value):
    return isinstance(value, str) and bool(SHA256_RE.match(value))
