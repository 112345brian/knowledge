"""Port and records of the claims source's record-shaped repository contract.

Client-owned: `ClaimRepository` and its records used to sit in the core's `build_repositories_port` and
`build_records`. They were never adopted by a production adapter (the claims step reaches `claims` through
`step_stores_port.insert_claim`); they are kept as they were for the claims collapse to decide. The generic claims
audit (`claims_audit`, `claims_rules`, `claims_store`) is NOT client logic: the `claims` and `claim_facts` tables
are in the core schema and the audit runs with no client source enabled.

Domain service ring: protocol and frozen values, no I/O.
"""
from dataclasses import dataclass
from typing import NewType, Optional, Protocol

ClaimId = NewType("ClaimId", int)


@dataclass(frozen=True)
class NewClaim:
    statement: str
    notes: Optional[str]
    inference_type: Optional[str]
    warrant: Optional[str]
    qualifier: Optional[str]


class ClaimRepository(Protocol):
    def add(self, claim: NewClaim) -> ClaimId: ...
    def link_facts_by_prefix(self, claim_id: ClaimId, prefix: str, role: str, note: Optional[str]) -> int:
        """The claim-to-fact link rows for every fact whose statement starts with `prefix` (LIKE, so `%`/`_` in a prefix are
        wildcards, as today); returns how many were linked."""
