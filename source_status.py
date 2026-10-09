"""Source status over time, editions and source-to-source relations (#41). Library only;
`knowledge.py audit-source-status` wraps the audit.

Why: the premise audit (#1) follows claim -> fact, but nothing followed fact -> source. A retracted paper or a
replaced textbook edition should flag the facts built on it. Archival practice treats a record's reliability as
something that can change; Crossref's Crossmark names the editorial update types, of which we keep the ones
that change what a reader should do.

On `sources`:
  status                  active | corrected | expression-of-concern | retracted | superseded  (default active)
  status_date             when that status began, YYYY / YYYY-MM / YYYY-MM-DD, or NULL (never invented)
  status_note             free text, e.g. the notice URL (private: left out of the normal-only DB)
  edition                 free text ("2nd edition")
  original_published_date the work's first publication, because published_date is the edition's date
Table `source_relations(source_id, relation, related_source_id)`: "source_id <relation> related_source_id":
  replaces        the first source replaces the second (the second is superseded, whatever its own status says)
  is-version-of   the first is a version (preprint, translation, reprint) of the second
Both relations are directed, may not relate a source to itself, and may not form a cycle.

Where the data lives: sources are loaded from vault note frontmatter (step 02) and manual_sources.json
(step 01), so status is a plain field read from there, with `status_date`, and git is its history. There is
NO revision log for sources: that would be a second place to keep the same fact. What would change this:
status changes that need a reason, an author, or retroactive "what did we believe on date X" answers for
sources (then sources need the append-only treatment facts got in #30).

The audit lists active/pending facts that cite a source whose status is retracted, expression-of-concern, or
superseded WITH a replacement (a `replaces` relation points at it; chains are followed to the newest
source). Sources marked superseded with no replacement are reported separately, not as rows.
"""
import re

import validtime

STATUS_VALUES = ("active", "corrected", "expression-of-concern", "retracted", "superseded")  # keep in sync with schema.sql
RELATIONS = ("replaces", "is-version-of")  # keep in sync with schema.sql
DEFAULT_STATUS = "active"
REASONS = ("retracted", "expression-of-concern", "superseded")
_FIELDS = ("status", "status_date", "status_note", "edition", "original_published_date")


class SourceStatusError(Exception):
    """A source's status fields or relations are invalid. The message names the source."""


def _text(value, where, field):
    if value is None:
        return None
    if not isinstance(value, str):
        raise SourceStatusError(f"{where}: {field} must be text, not {type(value).__name__}")
    return value.strip() or None


def normalize_fields(raw, where):
    """The five status columns from `raw` (a dict with those keys, any of them missing or None), validated.
    A source with none of them is `active` with a NULL status_date (legacy: no invented dates).
    Raises SourceStatusError naming `where` (the source) for an unknown status or a bad status_date."""
    out = {k: _text(raw.get(k), where, k) for k in _FIELDS}
    status = out["status"] or DEFAULT_STATUS
    if status not in STATUS_VALUES:
        raise SourceStatusError(f"{where}: status {status!r} must be one of {list(STATUS_VALUES)}")
    out["status"] = status
    if out["status_date"] is not None and not validtime.is_valid_boundary(out["status_date"]):
        raise SourceStatusError(f"{where}: status_date {out['status_date']!r} must be a real date: YYYY, YYYY-MM or YYYY-MM-DD")
    return out


def as_list(value):
    """A list of citekeys from frontmatter/JSON: None -> [], a list as is (wiki-link brackets stripped), a string
    split on commas (an inline `[a, b]` or `a, b`)."""
    if value is None:
        return []
    items = value if isinstance(value, list) else re.split(r",", str(value).strip().strip("[]"))
    out = []
    for item in items:
        if not isinstance(item, str):
            raise SourceStatusError(f"relation target {item!r} must be text")
        item = re.sub(r"^\[\[|\]\]$", "", item.strip().strip('"').strip("'").strip())
        if item:
            out.append(item)
    return out


def check_relations(relations, known_citekeys):
    """Validate (source citekey, relation, related citekey) triples against the citekeys that exist.
    Returns the triples de-duplicated, in input order. Raises SourceStatusError naming the sources for an unknown
    relation, an unknown citekey, a self-relation or a cycle (per relation type)."""
    known = set(known_citekeys)
    seen, out = set(), []
    for a, rel, b in relations:
        if rel not in RELATIONS:
            raise SourceStatusError(f"source {a!r}: relation {rel!r} must be one of {list(RELATIONS)}")
        for who in (a, b):
            if who not in known:
                raise SourceStatusError(f"source relation {a!r} {rel} {b!r}: no source has the citekey {who!r}")
        if a == b:
            raise SourceStatusError(f"source {a!r} cannot {rel.replace('-', ' ')} itself")
        if (a, rel, b) not in seen:
            seen.add((a, rel, b))
            out.append((a, rel, b))
    for rel in RELATIONS:
        graph = {}
        for a, r, b in out:
            if r == rel:
                graph.setdefault(a, []).append(b)
        state = {}

        def visit(node, path):
            state[node] = 1
            for nxt in graph.get(node, ()):
                if state.get(nxt) == 1:
                    cycle = path[path.index(nxt):] + [nxt] if nxt in path else [node, nxt]
                    raise SourceStatusError(f"{rel} cycle among sources: {' -> '.join(cycle)}")
                if nxt not in state:
                    visit(nxt, path + [nxt])
            state[node] = 2

        for start in list(graph):
            if start not in state:
                visit(start, [start])
    return out


# ------------------------------------------------------------------ the audit

def _replacers(db):
    """{source id: [ids of sources that replace it]}."""
    out = {}
    for sid, rid in db.execute("SELECT related_source_id, source_id FROM source_relations WHERE relation = 'replaces'"):
        out.setdefault(sid, []).append(rid)
    return out


def latest_replacements(sid, replacers):
    """The newest sources that (transitively) replace `sid`, following `replaces` edges: [] when nothing does.
    A chain A <- B <- C gives [C]. Relations are acyclic (check_relations), but a cycle is guarded anyway."""
    found, seen, stack = [], {sid}, [sid]
    while stack:
        cur = stack.pop()
        nxt = [r for r in replacers.get(cur, ()) if r not in seen]
        if not nxt and cur != sid:
            found.append(cur)
        for r in nxt:
            seen.add(r)
            stack.append(r)
    return sorted(set(found))


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
