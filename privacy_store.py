"""Adapter for the privacy rules (driven side of the privacy domain).

`privacy.py` is the domain: pure rule parsing, edits and the visibility resolver, with no file,
database or git access. This module is the only place that touches the outside world for those
rules: the rules file in PRIVATE_DATA_DIR (load/save) and the knowledge.db connection
(apply_rules_to_db). It does not commit; the CLI wraps writes with private_git (#10).
"""
import contextlib
import json
import os
import stat
import uuid
from dataclasses import replace

import entities_store as entities_lib
from privacy import PrivacyRulesError, Rules, RULES_FILENAME, VERSION, parse_rules, resolve_visibility, subject_chain


def rules_path(data_dir=None):
    if data_dir is None:
        from paths import PRIVATE_DATA_DIR  # lazy: keeps this module importable without a private repo
        data_dir = PRIVATE_DATA_DIR
    return os.path.join(data_dir, RULES_FILENAME)


def load_rules(path=None):
    """Rules from `path` (default: PRIVATE_DATA_DIR/privacy_rules.json). Absent file -> empty
    rules. A file that is unreadable, corrupt JSON or not an object raises PrivacyRulesError."""
    path = rules_path() if path is None else path
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return _with_entities(Rules(), path)
    except json.JSONDecodeError as e:
        raise PrivacyRulesError(f"{path} is not valid JSON ({e}); left untouched") from e
    except (OSError, UnicodeDecodeError) as e:
        raise PrivacyRulesError(f"could not read {path}: {e}") from e
    return _with_entities(parse_rules(data, source=path), path)


def _with_entities(rules, rules_file):
    """`rules` plus the private entities from entities.json beside the rules file (#42). An absent file
    adds none; a corrupt one raises, so a broken entity list fails closed instead of silently matching nothing."""
    try:
        found = entities_lib.read_file(os.path.join(os.path.dirname(os.path.abspath(rules_file)), entities_lib.ENTITIES_FILENAME))
    except entities_lib.EntitiesError as e:
        raise PrivacyRulesError(str(e)) from None
    if not found:
        return rules
    return replace(rules, entities=entities_lib.private_terms(found))


def save_rules(rules, path):
    """Atomic write (temp file + os.replace), keys sorted so diffs in knowledge-private are small."""
    data = {"version": VERSION,
            "subject_tags": {k: rules.subject_tags[k] for k in sorted(rules.subject_tags)},
            "keywords": sorted(rules.keywords)}
    # mode 0o666 at creation: the kernel applies the umask (os.umask(0) would change it process-wide)
    tmp = f"{os.path.abspath(path)}.{uuid.uuid4().hex}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ----------------------------------------------------------------- applying to a built db

def _rules_with_db_context(con, rules):
    rows = con.execute("SELECT s.name, p.name FROM subjects s LEFT JOIN subjects p ON p.id = s.parent_id").fetchall()
    parents = {name: parent for name, parent in rows}
    return rules.with_context(parents=parents, known_subjects=set(parents))


def apply_rules_to_db(con, rules):
    """Re-apply the CURRENT rules to every fact in `con` (used at the end of 04 and 11, so a rebuild
    retroactively privatizes old facts). Raise-only: a fact stored 'private' stays private even if
    the rule that once caught it is gone. Also writes subjects.private (1 for every subject whose
    own tag or an ancestor's tag is private). Returns {"raised": [(fact_id, explanation)...],
    "private_subjects": n}. Does not commit."""
    ctx = _rules_with_db_context(con, rules)
    private_subjects = 0
    for (name,) in con.execute("SELECT name FROM subjects").fetchall():
        chain, cyclic = subject_chain(name, ctx)
        flag = 1 if cyclic or any(rules.subject_tags.get(n) == "private" for n in chain) else 0
        con.execute("UPDATE subjects SET private = ? WHERE name = ?", (flag, name))
        private_subjects += flag
    raised = []
    rows = con.execute(
        """SELECT f.id, s.name, f.statement, f.visibility, f.notes, f.trust_rationale, f.recheck_rationale,
                  f.source_quote, f.applies_to,
                  (SELECT group_concat(COALESCE(fs.locator, '') || ' ' || COALESCE(fs.quote, ''), char(10))
                   FROM fact_sources fs WHERE fs.fact_id = f.id)
           FROM facts f JOIN subjects s ON s.id = f.subject_id""").fetchall()
    for fact_id, subject, statement, stored, *extra in rows:
        res = resolve_visibility(subject, statement, stored, ctx, extra_text=extra)
        if res.visibility == "private" and stored != "private":
            con.execute("UPDATE facts SET visibility = 'private' WHERE id = ?", (fact_id,))
            raised.append((fact_id, res.explain()))
    return {"raised": raised, "private_subjects": private_subjects}


def data_dir_of(path):
    """The directory a rules file lives in."""
    return os.path.dirname(os.path.abspath(path))


def rules_file_exists(path):
    return os.path.exists(path)


def existing_ancestor(directory):
    """The nearest existing directory at or above `directory` (the data dir may not exist before the first rule)."""
    probe = directory
    while not os.path.isdir(probe):
        probe = os.path.dirname(probe)
    return probe


def make_dirs(directory):
    os.makedirs(directory, exist_ok=True)
