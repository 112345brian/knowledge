"""Adapter for the source-status audit (#41): the SQL behind `knowledge.py audit-source-status`. Reads `sources`,
`source_relations` and `fact_sources`; the rules (normalization, relation checks, following `replaces` chains)
are in `source_status` (domain). Never writes.
"""
from source_status import latest_replacements


def _replacers(db):
    """{source id: [ids of sources that replace it]}."""
    out = {}
    for sid, rid in db.execute("SELECT related_source_id, source_id FROM source_relations WHERE relation = 'replaces'"):
        out.setdefault(sid, []).append(rid)
    return out


def audit_source_status(db):
    """One dict per (fact, flagged source), ordered by fact_id then source_id: fact_id, source_key, fact_statement,
    source_id, citekey, source_name, reason ('retracted' | 'expression-of-concern' | 'superseded'), status,
    status_date, status_note, edition, replacement ([{id, citekey, name}] for superseded, else []),
    derived (True when 'superseded' comes only from a `replaces` relation, the source's own status being something
    else). Only facts that are active or pending are considered. Never writes. `db` is an open sqlite3 connection."""
    replacers = _replacers(db)
    src = {r[0]: r for r in db.execute("SELECT id, citekey, name, status, status_date, status_note, edition FROM sources")}
    out = []
    rows = db.execute(
        """SELECT f.id, f.source_key, f.statement, fs.source_id
           FROM fact_sources fs JOIN facts f ON f.id = fs.fact_id
           WHERE f.status IN ('active', 'pending') ORDER BY f.id, fs.source_id""").fetchall()
    for fact_id, key, statement, sid in rows:
        _, citekey, name, status, status_date, status_note, edition = src[sid]
        replacement = [{"id": r, "citekey": src[r][1], "name": src[r][2]} for r in latest_replacements(sid, replacers)]
        derived = False
        if status in ("retracted", "expression-of-concern"):
            reason = status
        elif replacement:  # status 'superseded' with a replacement, or any status that a `replaces` relation overrides
            reason, derived = "superseded", status != "superseded"
        else:
            continue
        out.append({"fact_id": fact_id, "source_key": key, "fact_statement": statement, "source_id": sid, "citekey": citekey,
                    "source_name": name, "reason": reason, "status": status, "status_date": status_date,
                    "status_note": status_note, "edition": edition,
                    "replacement": replacement if reason == "superseded" else [], "derived": derived})
    return out


def superseded_without_replacement(db):
    """Sources marked superseded that nothing replaces, and that an active/pending fact cites: the audit cannot
    say what to move to, so they are listed here instead of as rows. [{source_id, citekey, name, fact_ids}]"""
    replacers = _replacers(db)
    out = {}
    for sid, citekey, name, fid in db.execute(
            """SELECT s.id, s.citekey, s.name, f.id FROM sources s
               JOIN fact_sources fs ON fs.source_id = s.id JOIN facts f ON f.id = fs.fact_id
               WHERE s.status = 'superseded' AND f.status IN ('active', 'pending') ORDER BY s.id, f.id"""):
        if not latest_replacements(sid, replacers):
            out.setdefault(sid, {"source_id": sid, "citekey": citekey, "name": name, "fact_ids": []})["fact_ids"].append(fid)
    return list(out.values())
