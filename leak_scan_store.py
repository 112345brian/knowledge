"""SQLite adapter for `normal_db_port.LeakScan`: it reads every cell of a normal DB and the private rows of the
full DB, read-only. It decides nothing; which marker texts those rows become, and what counts as a leak, are
`leak_rules`. Stateless; the module is the adapter."""
import os
import sqlite3

from leak_rules import PrivateMaterial


def _open_readonly(path):
    return sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)


def cells(path):
    """(`table.column`, value) for every cell of every table of the database at `path`. The connection lives
    only while the iterator is consumed."""
    con = _open_readonly(path)
    try:
        for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
            if not cols:
                continue
            for row in con.execute(f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} FROM "{table}"'):
                for col, cell in zip(cols, row):
                    yield f"{table}.{col}", cell
    finally:
        con.close()


def private_material(full_path):
    full = _open_readonly(full_path)
    try:
        def q(sql):
            return full.execute(sql).fetchall()

        # strings that are legitimately in the normal tier are never markers
        private_fact = ("(f.visibility != 'normal' OR EXISTS (SELECT 1 FROM subjects s WHERE s.id = f.subject_id AND s.private = 1))")
        normal_fact = "NOT " + private_fact
        legit = set()
        for sql in (f"SELECT statement, trust_rationale, source_quote FROM facts f WHERE {normal_fact}",
                    f"SELECT fs.quote, fs.locator, NULL FROM fact_sources fs JOIN facts f ON f.id = fs.fact_id WHERE {normal_fact}",
                    "SELECT name, description, url FROM sources",
                    "SELECT name, NULL, NULL FROM subjects WHERE private = 0"):
            for row in q(sql):
                legit.update(c for c in row if isinstance(c, str))
        return PrivateMaterial(
            legit=legit,
            private_facts=q(f"SELECT f.id, f.statement, f.notes, f.source_quote, f.trust_rationale FROM facts f WHERE {private_fact}"),
            private_citations=q("SELECT fs.fact_id, fs.locator, fs.quote FROM fact_sources fs JOIN facts f ON f.id = fs.fact_id "
                                f"WHERE {private_fact}"),
            private_revisions=q("SELECT r.statement FROM fact_revisions r JOIN facts f ON f.id = r.fact_id "
                                f"WHERE r.visibility != 'normal' OR {private_fact}"),
            claims=q("SELECT id, statement, notes FROM claims"),
            vault_paths=q("SELECT path FROM vault_files"),
            origin_paths=q("SELECT origin_path FROM sources"),
            build_commits=q("SELECT code_commit, private_commit FROM build_info"),
            build_input_keys=q("SELECT input_key FROM build_inputs"),
            status_notes=q("SELECT status_note FROM sources"),
            where_from=q("SELECT where_from FROM sources UNION SELECT where_from FROM vault_files"),
            file_hashes=q("SELECT content_sha256 FROM vault_files UNION SELECT content_sha256 FROM sources "
                          "UNION SELECT extracted_from_sha256 FROM facts"),
            private_subject_names=q("SELECT name FROM subjects WHERE private = 1"),
            private_entities=q("SELECT canonical_name, name_norm, entity_key, external_id, notes FROM entities WHERE private = 1"),
            private_entity_aliases=q("SELECT a.alias, a.alias_norm FROM entity_aliases a JOIN entities e ON e.id = a.entity_id "
                                     "WHERE e.private = 1"),
            private_subject_descriptions=q("SELECT description FROM subjects WHERE private = 1"),
            private_subject_aliases=q("SELECT a.alias FROM subject_aliases a JOIN subjects s ON s.id = a.subject_id "
                                      "WHERE s.private = 1"),
        )
    finally:
        full.close()
