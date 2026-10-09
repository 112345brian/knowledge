#!/usr/bin/env python3
"""Leak test for knowledge-normal.db (#21). Fails loudly when a private marker is found.

Two modes:
    python3 leak_test.py
        Self-test: builds a fixture full DB (the real schema.sql) holding known private markers
        plus normal facts, builds the normal DB, and scans it. Also proves the scan can fail by
        injecting a marker into a copy and requiring it to be flagged. Exit 0 only if both hold.
    python3 leak_test.py --full FULL_DB --normal NORMAL_DB
        Real-build check: derives markers from everything private in FULL_DB (non-normal facts,
        facts on private subjects, their notes/quotes/citations, claims, vault paths, source
        origin paths, private subject names, private revisions) and scans NORMAL_DB. build.py runs
        the same check right after it builds the normal DB.

A marker is looked for in (1) every text/blob cell of every table, with the table named, and
(2) the RAW BYTES of the db file (case-insensitive, NFC and NFD forms), which also catches text
left in free pages or FTS shadow tables.
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
MIN_MARKER_LEN = 6  # shorter derived strings are too generic to be a meaningful marker


def _variants(marker):
    out = set()
    for form in ("NFC", "NFD"):
        out.add(unicodedata.normalize(form, marker).lower())
    return out


def scan(normal_path, markers):
    """markers: iterable of strings (or {label: string}). Returns [(label, marker, where)]."""
    if not isinstance(markers, dict):
        markers = {m: m for m in markers}
    leaks = []
    for label, marker in markers.items():
        if not marker or not marker.strip():
            raise ValueError(f"empty marker {label!r}: it would match everything")
    with open(normal_path, "rb") as f:
        raw = f.read().lower()
    for label, marker in markers.items():
        if any(v.encode("utf-8") in raw for v in _variants(marker)):
            leaks.append((label, marker, "raw file bytes"))
    con = sqlite3.connect(f"file:{os.path.abspath(normal_path)}?mode=ro", uri=True)
    try:
        for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
            if not cols:
                continue
            for row in con.execute(f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} FROM "{table}"'):
                for col, cell in zip(cols, row):
                    if isinstance(cell, bytes):
                        cell = cell.decode("utf-8", "ignore")
                    if not isinstance(cell, str):
                        continue
                    folded = unicodedata.normalize("NFC", cell).lower()
                    for label, marker in markers.items():
                        if any(unicodedata.normalize("NFC", v) in folded for v in _variants(marker)):
                            leaks.append((label, marker, f"{table}.{col}"))
    finally:
        con.close()
    return leaks


def format_leaks(leaks):
    return "\n".join(f"  LEAK {label!r} found in {where}" for label, _m, where in leaks)


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
            # `shareable`: text copied from a private fact that a normal fact may legitimately also hold
            # (e.g. the same quote from a shared source); claims, paths and subject names never are.
            if isinstance(value, str) and len(value.strip()) >= MIN_MARKER_LEN and not (shareable and value in legit):
                markers.setdefault(f"{label}: {value[:40]}", value)

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
        for (n,) in full.execute("SELECT status_note FROM sources"):      # #41: a notice URL may be private; never copied
            add("source status note", n)
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


# ----------------------------------------------------------------------------- fixture

MARKERS = {
    "private fact text": "Velthorn hormone panel result was alarming",
    "rules-file name in untagged subject": "Marnoq Fothergill",
    "private fact source quote": "quenchwhistle verbatim private quote",
    "private subject name": "zephyr-family-matters",
    "private entity name": "Quillon Fernsby",
    "private entity alias": "Q-Fern-Alias",
    "private entity notes": "fernsby-secret-notes about a relative",
    "private subject description": "quillfeather-secret-description of the family topic",
    "private subject alias": "zephyr-secret-alias",
    "source status note": "https://private.example/zq-retraction-notice-7731",
    "vault path": "Vault/Journal/zanzibar-secret-note.md",
    "file hash": "9f3c1a7be25d48e0a6b1c7d3f09e82a45b6d1e7c30f8a29b4c5d6e7f8091a2b3",
    "claim text": "Therefore Grumbleton should change his life",
    "private fact notes": "plover-notes-private-scribble",
    "private source origin path": "Vault/sources/zinnia-private-origin.md",
    "older private revision": "Xylophone old private wording",
    "private-subject fact under normal-looking fact": "Underchild secret zeta",
    "unicode private": "Zoë Ångström-Müller's diagnosis",
}
NORMAL_STATEMENTS = ("Creatine monohydrate is well studied for strength.", "Sleep duration affects recovery: café study.")


def build_fixture(directory):
    """Make a full DB with the real schema, all MARKERS private, and normal facts. Returns its path."""
    path = os.path.join(directory, "fixture-full.db")
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys = ON")
    with open(os.path.join(HERE, "schema.sql")) as f:
        con.executescript(f.read())
    M = MARKERS
    ex = con.execute
    ex("INSERT INTO subjects (id, name, domain, parent_id, private) VALUES (1, 'nutrition', 'health', NULL, 0)")
    ex("INSERT INTO subjects (id, name, domain, parent_id, private) VALUES (2, 'sleep', 'health', 1, 0)")
    ex("INSERT INTO subjects (id, name, domain, parent_id, private) VALUES (3, ?, 'life', NULL, 1)", (M["private subject name"],))
    ex("INSERT INTO subjects (id, name, domain, parent_id, private) VALUES (4, 'underchild', 'life', 3, 1)")
    ex("INSERT INTO subjects (id, name, domain, parent_id, private) VALUES (5, 'untagged-topic', 'health', NULL, 0)")
    ex("UPDATE subjects SET description = ? WHERE id = 3", (M["private subject description"],))
    ex("INSERT INTO subject_aliases (subject_id, alias) VALUES (3, ?)", (M["private subject alias"],))
    ex("UPDATE subjects SET description = 'Sleep habits and duration', parent_relation = 'part-of' WHERE id = 2")
    ex("INSERT INTO subject_aliases (subject_id, alias) VALUES (2, 'rest')")
    ex("INSERT INTO entities (id, entity_key, canonical_name, name_norm, type, private, notes) VALUES (1, 'quillon-fernsby', ?, ?, 'person', 1, ?)",
       (M["private entity name"], M["private entity name"].casefold(), M["private entity notes"]))
    ex("INSERT INTO entity_aliases (entity_id, alias, alias_norm) VALUES (1, ?, ?)", (M["private entity alias"], M["private entity alias"].casefold()))
    ex("INSERT INTO entities (id, entity_key, canonical_name, name_norm, type, private, external_id, notes) VALUES (2, 'acme-labs', 'Acme Labs', 'acme labs', 'organization', 0, 'wikidata:Q1', 'A public lab')")
    ex("INSERT INTO entity_aliases (entity_id, alias, alias_norm) VALUES (2, 'Acme', 'acme')")
    ex("INSERT INTO vault_files (id, path, content_sha256, size_bytes, file_state) VALUES (1, ?, ?, 10, 'present')",
       (M["vault path"], M["file hash"]))
    ex("INSERT INTO publishers (id, name) VALUES (1, 'Journal of Fixtures')")
    ex("INSERT INTO publishers (id, name) VALUES (2, 'Private Press')")
    ex("INSERT INTO authors (id, name) VALUES (1, 'A. Public'), (2, 'P. Rivate')")
    ex("INSERT INTO sources (id, citekey, name, source_type, publisher_id, origin_path) VALUES "
       "(1, 'pub2020', 'A shared study', 'primary', 1, ?)", (M["private source origin path"],))
    ex("INSERT INTO sources (id, citekey, name, source_type, publisher_id, origin_path) VALUES "
       "(2, 'priv2021', 'Only cited privately', 'primary', 2, 'x')")
    ex("INSERT INTO source_authors (source_id, author_id) VALUES (1, 1), (2, 2)")
    ex("UPDATE sources SET status = 'corrected', status_date = '2025-03', status_note = ?, edition = '2nd edition' WHERE id = 1", (M["source status note"],))
    ex("INSERT INTO source_relations (source_id, relation, related_source_id) VALUES (1, 'replaces', 2)")

    def fact(fid, subject, statement, vis, key, notes=None, quote=None, origin=None, personal=1):
        ex("INSERT INTO facts (id, subject_id, statement, is_personal, trust_level, visibility, source_key, notes, "
           "source_quote, origin_file_id, freshness) VALUES (?, ?, ?, ?, 'medium', ?, ?, ?, ?, ?, 'unreviewed')",
           (fid, subject, statement, personal, vis, key, notes, quote, origin))
        ex("INSERT INTO fact_revisions (fact_id, source_key, revision, changed_at, changed_via, statement, trust_level, "
           "status, visibility, session_id, change_reason, notes) VALUES (?, ?, 1, '2026-01-01T00:00:00+00:00', 'ingest', ?, 'medium', "
           "'active', ?, 'sess-secret-1', 'because reasons', ?)", (fid, key, statement, vis, notes))

    fact(1, 1, NORMAL_STATEMENTS[0], "normal", "k-normal-1", origin=1, personal=0)
    fact(2, 2, NORMAL_STATEMENTS[1], "normal", "k-normal-2")
    fact(3, 1, M["private fact text"], "private", "k-priv-1", notes=M["private fact notes"], quote=M["private fact source quote"], origin=1)
    # name from the rules file, untagged subject: the resolver stored it private
    fact(4, 5, f"{M['rules-file name in untagged subject']} visited last week", "private", "k-priv-2")
    # under a private subject; stored (wrongly, as if the floor failed) normal -- the subject floor still keeps it out
    fact(5, 4, M["private-subject fact under normal-looking fact"], "normal", "k-floor-1")
    fact(6, 3, "Another private-subject fact about the family", "private", "k-priv-3")
    fact(7, 1, M["unicode private"], "private", "k-priv-4")
    # #42: the private fact mentions the private entity; the normal fact mentions the public one
    ex("INSERT INTO fact_entities (fact_id, entity_id) VALUES (3, 1)")
    ex("INSERT INTO fact_entities (fact_id, entity_id) VALUES (1, 2)")
    # #38: a normal fact and a normal-included source both carry the hash; neither column is copied
    ex("UPDATE facts SET extracted_from_sha256 = ? WHERE id = 1", (M["file hash"],))
    ex("UPDATE sources SET content_sha256 = ?, size_bytes = 10, file_state = 'present' WHERE id = 1", (M["file hash"],))
    # history: older revision private, current normal; and older normal, current private
    ex("INSERT INTO fact_revisions (fact_id, source_key, revision, changed_at, changed_via, statement, trust_level, status, visibility) "
       "VALUES (1, 'k-normal-1', 2, '2026-02-01T00:00:00+00:00', 'cli', 'Creatine monohydrate is well studied for strength.', 'high', 'active', 'normal')")
    ex("INSERT INTO fact_revisions (fact_id, source_key, revision, changed_at, changed_via, statement, trust_level, status, visibility) "
       "VALUES (2, 'k-normal-2', 2, '2026-02-02T00:00:00+00:00', 'cli', ?, 'high', 'active', 'private')", (M["older private revision"],))
    # citations: source 1 cited by a normal fact and a private fact; source 2 by a private fact only
    ex("INSERT INTO fact_sources (fact_id, source_id, locator, quote) VALUES (1, 1, 'p. 4', 'public quote about creatine')")
    ex("INSERT INTO fact_sources (fact_id, source_id, locator, quote) VALUES (3, 1, 'p. 9', ?)", (M["private fact source quote"],))
    ex("INSERT INTO fact_sources (fact_id, source_id, locator, quote) VALUES (6, 2, 'p. 2', 'private press quote')")
    ex("INSERT INTO claims (id, statement) VALUES (1, ?)", (M["claim text"],))
    # non-fact tables with private-ish rows
    ex("INSERT INTO artists (id, name) VALUES (1, 'Secret Band')")
    con.commit()
    con.close()
    return path


def self_test(workdir=None, verbose=True):
    """Returns a list of problems (empty = pass). Builds the fixture and the normal DB, scans it,
    then proves the scan can fail by injecting each marker into a copy."""
    import privacy
    import normal_db
    problems = []
    own = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix="leak-test-")
    try:
        full = build_fixture(workdir)
        out_dir = os.path.join(workdir, "out")
        os.makedirs(out_dir)
        rules = privacy.Rules(subject_tags={M: "private" for M in [MARKERS["private subject name"]]},
                              keywords=(MARKERS["rules-file name in untagged subject"].split()[0].lower(),))
        path, counts = normal_db.build_normal_atomic(full, out_dir, rules)
        if verbose:
            print("normal DB row counts:\n" + normal_db.format_counts(counts))
        for label, _m, where in scan(path, MARKERS):
            problems.append(f"marker {label!r} leaked into the normal DB ({where})")
        for leak in scan_against_full(path, full):
            problems.append(f"derived marker {leak[0]!r} leaked ({leak[2]})")
        # the scan must be able to fail: inject each marker into a copy and require a flag
        for label, marker in MARKERS.items():
            bad = os.path.join(workdir, "injected.db")
            shutil.copy(path, bad)
            c = sqlite3.connect(bad)
            c.execute("UPDATE facts SET trust_rationale = ? WHERE id = (SELECT MIN(id) FROM facts)", (f"x {marker} x",))
            c.commit()
            c.close()
            if not scan(bad, {label: marker}):
                problems.append(f"the scan did NOT flag an injected marker ({label!r}); the test cannot fail")
            os.remove(bad)
    finally:
        if own:
            shutil.rmtree(workdir, ignore_errors=True)
    return problems


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv:
        if len(argv) != 4 or argv[0] != "--full" or argv[2] != "--normal":
            print("usage: leak_test.py [--full FULL_DB --normal NORMAL_DB]", file=sys.stderr)
            return 2
        markers = derive_markers(argv[1])
        leaks = scan(argv[3], markers)
        if leaks:
            print(f"LEAK TEST FAILED ({len(leaks)} finding(s)):\n{format_leaks(leaks)}", file=sys.stderr)
            return 1
        print(f"leak test passed: {len(markers)} derived markers, none found in {argv[3]}")
        return 0
    problems = self_test()
    if problems:
        print("LEAK TEST FAILED:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    print(f"leak test passed: {len(MARKERS)} fixture markers absent from rows and raw bytes; scan proven able to fail")
    return 0


if __name__ == "__main__":
    sys.exit(main())
