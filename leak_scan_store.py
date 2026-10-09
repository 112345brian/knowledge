"""Read-only database and file scanner for the normal-tier leak check."""
import os
import sqlite3

from leak_rules import (MIN_MARKER_LEN, add_marker, check_markers, leaks_in_bytes,
                        leaks_in_cell)


def scan(normal_path, markers):
    """markers: iterable of strings (or {label: string}). Returns [(label, marker, where)]."""
    markers = check_markers(markers)
    with open(normal_path, "rb") as f:
        raw = f.read().lower()
    leaks = leaks_in_bytes(raw, markers)
    con = sqlite3.connect(f"file:{os.path.abspath(normal_path)}?mode=ro", uri=True)
    try:
        for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
            if not cols:
                continue
            for row in con.execute(f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} FROM "{table}"'):
                for col, cell in zip(cols, row):
                    leaks.extend(leaks_in_cell(cell, f"{table}.{col}", markers))
    finally:
        con.close()
    return leaks


def derive_markers(full_path):
    """{label: text} for everything in the full DB that must never reach the normal tier."""
    full = sqlite3.connect(f"file:{os.path.abspath(full_path)}?mode=ro", uri=True)
    try:
        markers = {}
        # strings that are legitimately in the normal tier are never markers
        private_fact = ("(f.visibility != 'normal' OR EXISTS (SELECT 1 FROM subjects s WHERE s.id = f.subject_id AND s.private = 1))")
        normal_fact = "NOT " + private_fact
        legit = set()
        for q in (f"SELECT statement, trust_rationale, source_quote FROM facts f WHERE {normal_fact}",
                  f"SELECT fs.quote, fs.locator, NULL FROM fact_sources fs JOIN facts f ON f.id = fs.fact_id WHERE {normal_fact}",
                  "SELECT name, description, url FROM sources",
                  "SELECT name, NULL, NULL FROM subjects WHERE private = 0"):
            for row in full.execute(q):
                legit.update(c for c in row if isinstance(c, str))

        def add(label, value, shareable=False):
            add_marker(markers, label, value, legit, shareable)

        for fid, st, notes, quote, rat in full.execute(
                f"SELECT f.id, f.statement, f.notes, f.source_quote, f.trust_rationale FROM facts f WHERE {private_fact}"):
            add(f"private fact {fid} statement", st, True)
            add(f"private fact {fid} notes", notes, True)
            add(f"private fact {fid} source_quote", quote, True)
            add(f"private fact {fid} trust_rationale", rat, True)
        for fid, loc, quote in full.execute(
                f"SELECT fs.fact_id, fs.locator, fs.quote FROM fact_sources fs JOIN facts f ON f.id = fs.fact_id WHERE {private_fact}"):
            add(f"private fact {fid} citation locator", loc, True)
            add(f"private fact {fid} citation quote", quote, True)
        for (st,) in full.execute("SELECT r.statement FROM fact_revisions r JOIN facts f ON f.id = r.fact_id "
                                  f"WHERE r.visibility != 'normal' OR {private_fact}"):
            add("private revision statement", st, True)
        for cid, st, notes in full.execute("SELECT id, statement, notes FROM claims"):
            add(f"claim {cid} statement", st)
            add(f"claim {cid} notes", notes)
        for (p,) in full.execute("SELECT path FROM vault_files"):
            add("vault path", p)
        for (p,) in full.execute("SELECT origin_path FROM sources"):
            add("source origin_path", p)
        for (a, b) in full.execute("SELECT code_commit, private_commit FROM build_info"):      # #47
            add("code commit", a)
            add("private commit", b)
        for (k,) in full.execute("SELECT input_key FROM build_inputs"):
            add("build input key", k)
        for (n,) in full.execute("SELECT status_note FROM sources"):      # #41: a notice URL may be private; never copied
            add("source status note", n)
        for (u,) in full.execute("SELECT where_from FROM sources UNION SELECT where_from FROM vault_files"):   # #46: download URLs reveal interests
            add("where_from URL", u)
        # #38 fixity: hashes identify private files and must never reach the normal DB.
        for (h,) in full.execute("SELECT content_sha256 FROM vault_files UNION SELECT content_sha256 FROM sources "
                                 "UNION SELECT extracted_from_sha256 FROM facts"):
            add("file hash", h)
        for (n,) in full.execute("SELECT name FROM subjects WHERE private = 1"):
            add("private subject name", n)
        # #43: a private subject's description and aliases are as private as its name
        # #42: a private entity's name, aliases, notes and id are as private as the facts that mention it
        for (n, norm, key, ext, notes) in full.execute("SELECT canonical_name, name_norm, entity_key, external_id, notes FROM entities WHERE private = 1"):
            for label, value in (("private entity name", n), ("private entity name (normalized)", norm), ("private entity key", key),
                                 ("private entity external id", ext), ("private entity notes", notes)):
                add(label, value)
        for (a, an) in full.execute("SELECT a.alias, a.alias_norm FROM entity_aliases a JOIN entities e ON e.id = a.entity_id WHERE e.private = 1"):
            add("private entity alias", a)
            add("private entity alias (normalized)", an)
        for (d,) in full.execute("SELECT description FROM subjects WHERE private = 1"):
            add("private subject description", d)
        for (a,) in full.execute("SELECT a.alias FROM subject_aliases a JOIN subjects s ON s.id = a.subject_id WHERE s.private = 1"):
            add("private subject alias", a)
        return markers
    finally:
        full.close()


def scan_against_full(normal_path, full_path):
    return scan(normal_path, derive_markers(full_path))


