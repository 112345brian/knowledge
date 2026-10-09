"""Hand-authored broader claims, each citing the specific facts backing it.
Run last (needs facts to exist).

A claim may also carry a `warrant` (why the cited grounds support it) and a `qualifier` (how far it holds), #44.
`fact_match_prefixes` entries are either a statement prefix (a plain string: the fact is `grounds`) or a dict
{"prefix": ..., "role": "grounds" | "backing" | "rebuttal", "note": "why this fact is linked"}. Blank warrant,
qualifier and note are stored as NULL; an unknown role fails the build."""
import json
import os
import sqlite3
import sys

import claims_audit


def load_claims(data_dir=None):
    """Read optional claim data from the private companion repository."""
    if data_dir is None:
        from paths import PRIVATE_DATA_DIR
        data_dir = PRIVATE_DATA_DIR
    path = os.path.join(data_dir, "claims.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"{path}: cannot read claims: {e}") from e
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("claims"), list):
        raise ValueError(f"{path}: expected version 1 with a claims list")
    return payload["claims"]


def _blank_to_none(value, what):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{what} must be text, not {type(value).__name__}")
    return value.strip() or None


def _premise(entry, claim_statement):
    """(prefix, role, note) from a plain prefix string or a {"prefix", "role", "note"} dict."""
    if isinstance(entry, str):
        return entry, "grounds", None
    if not isinstance(entry, dict) or not isinstance(entry.get("prefix"), str) or not entry["prefix"]:
        raise ValueError(f"claim {claim_statement[:50]!r}: each fact_match_prefixes entry must be a string or a dict with a 'prefix'")
    role = entry.get("role", "grounds")
    if role not in claims_audit.ROLES:
        raise ValueError(f"claim {claim_statement[:50]!r}: role {role!r} must be one of {list(claims_audit.ROLES)}")
    return entry["prefix"], role, _blank_to_none(entry.get("note"), "note")


def run(con, claims=None, data_dir=None):
    claims = load_claims(data_dir) if claims is None else claims
    cur = con.cursor()
    for c in claims:
        cur.execute("INSERT INTO claims (statement, notes, inference_type, warrant, qualifier) VALUES (?, ?, ?, ?, ?)",
                    (c["statement"], c["notes"], c.get("inference_type"),
                     _blank_to_none(c.get("warrant"), "warrant"), _blank_to_none(c.get("qualifier"), "qualifier")))
        claim_id = cur.lastrowid
        for entry in c["fact_match_prefixes"]:
            prefix, role, note = _premise(entry, c["statement"])
            cur.execute(
                "INSERT INTO claim_facts (claim_id, fact_id, role, note) SELECT ?, id, ?, ? FROM facts WHERE statement LIKE ?",
                (claim_id, role, note, prefix + "%")
            )
    con.commit()
    print(f"[seed_claims] inserted {len(claims)} claims")


if __name__ == "__main__":
    con = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.db"))
    con.execute("PRAGMA foreign_keys = ON;")
    run(con)
    con.close()
