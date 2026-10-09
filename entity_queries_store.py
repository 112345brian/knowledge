"""Read-only entity and subject queries for the CLI."""
import fact_queries


def entity_link_counts():
    try:
        con = fact_queries.connect()
    except fact_queries.DatabaseNotFound:
        return {}
    try:
        return {key: count for key, count in con.execute(
            "SELECT e.entity_key, COUNT(fe.fact_id) FROM entities e "
            "LEFT JOIN fact_entities fe ON fe.entity_id = e.id GROUP BY e.id")}
    except Exception:
        return {}
    finally:
        con.close()


def known_subjects():
    try:
        con = fact_queries.connect()
    except fact_queries.DatabaseNotFound:
        return {}
    try:
        return {name: domain for name, domain in con.execute("SELECT name, domain FROM subjects")}
    except Exception:
        return {}
    finally:
        con.close()


def subject_rows(db=None):
    owns_connection = db is None
    con = fact_queries.connect() if owns_connection else db
    try:
        rows = [dict(row) for row in con.execute(
            """SELECT s.id, s.name, s.domain, p.name AS parent, s.parent_relation AS relation, s.description,
                      s.deprecated, r.name AS replaced_by,
                      (SELECT COUNT(*) FROM facts f WHERE f.subject_id = s.id) AS n_facts
               FROM subjects s LEFT JOIN subjects p ON p.id = s.parent_id
               LEFT JOIN subjects r ON r.id = s.replaced_by_subject_id ORDER BY s.name""")]
        aliases = {}
        for subject_id, alias in con.execute("SELECT subject_id, alias FROM subject_aliases ORDER BY alias"):
            aliases.setdefault(subject_id, []).append(alias)
        for row in rows:
            row["aliases"] = aliases.get(row["id"], [])
            row["deprecated"] = bool(row["deprecated"])
            if not row["parent"]:
                row["relation"] = None
        return rows
    finally:
        if owns_connection:
            con.close()
