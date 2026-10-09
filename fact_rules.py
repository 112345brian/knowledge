"""Pure vocabulary shared by the fact-writing modules (domain: no I/O).

The allowed values of the mutable fact fields and the shape of the stable identifiers. `add_fact`
(creating facts) and `revisions` (the change log) both validate against these, and neither may pull
the other in, so they live here. Keep in sync with the CHECKs in schema.sql.
"""
import re

VALID_TRUST = {"verified", "high", "medium", "low", "unverified", "disputed"}
VALID_VISIBILITY = {"private", "normal"}  # keep in sync with the CHECK on facts.visibility
# #7: every fact has a freshness (facts.freshness). A NEW fact derives 'recheck' (it has a
# recheck_by) or 'no-decay' (explicit assertion plus a written recheck_rationale); 'unreviewed' is
# legacy-only (facts that predate the column and were never reviewed) and is never accepted here.
FRESHNESS_VALUES = ("recheck", "no-decay", "unreviewed")  # keep in sync with the CHECK in schema.sql
VIA_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
# Stable identity of a fact across rebuilds (#30): revisions in the revision log point at it.
SOURCE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
