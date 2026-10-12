"""Adapter for the privacy rules (driven side of the privacy domain).

`privacy.py` is the domain: pure rule parsing, edits and the visibility resolver, with no file,
database or git access. This module is the only place that touches the outside world for those
rules: the rules file in PRIVATE_DATA_DIR (load/save) and the knowledge.db connection
(`SqlitePrivacyRepository`, `apply_rules_to_db`). It does not commit; the CLI wraps writes with private_git (#10).
"""
import contextlib
import json
import os
import stat
import uuid
from dataclasses import replace
from typing import Dict, List, Optional

import build_records as r
import entities_store as entities_lib
import fact_state
import locks
import privacy_floor_service
from privacy import PrivacyRulesError, Rules, RULES_FILENAME, VERSION, parse_rules
from privacy_floor_service import FloorFact

_CHUNK = 500   # well under SQLite's bound-parameter limit


def load_rules(path):
    """Rules from the privacy rules file at `path`. Absent file -> empty rules. A file that is unreadable,
    corrupt JSON or not an object raises PrivacyRulesError."""
    try:
        with locks.open_retrying(path, encoding="utf-8") as f:
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
        locks.replace_file(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ----------------------------------------------------------------- applying to a built db

class _FloorRows:
    """`privacy_floor_service.FloorRows` over a connection: reads and writes the rows the floor touches."""

    def __init__(self, con):
        self._con = con

    def subject_parents(self) -> Dict[str, Optional[str]]:
        rows = self._con.execute("SELECT s.name, p.name FROM subjects s LEFT JOIN subjects p ON p.id = s.parent_id").fetchall()
        return {name: parent for name, parent in rows}

    def mark_subject(self, name: str, private: bool) -> None:
        self._con.execute("UPDATE subjects SET private = ? WHERE name = ?", (1 if private else 0, name))

    def facts_to_scan(self) -> List[FloorFact]:
        rows = self._con.execute(
            """SELECT f.id, s.name, f.statement, f.visibility, f.notes, f.trust_rationale, f.recheck_rationale,
                      f.source_quote, f.applies_to,
                      (SELECT group_concat(COALESCE(fs.locator, '') || ' ' || COALESCE(fs.quote, ''), char(10))
                       FROM fact_sources fs WHERE fs.fact_id = f.id)
               FROM facts f JOIN subjects s ON s.id = f.subject_id""").fetchall()
        return [FloorFact(r.FactId(fact_id), subject, statement, stored, tuple(extra))
                for fact_id, subject, statement, stored, *extra in rows]

    def raise_to_private(self, fact_id: r.FactId) -> None:
        self._con.execute("UPDATE facts SET visibility = 'private' WHERE id = ?", (fact_id,))


class SqlitePrivacyRepository:
    """`build_repositories_port.PrivacyRepository` over a build connection. Which facts the rules raise is
    `privacy_floor_service`'s (and `privacy`'s); this reads and writes the rows. It never commits."""

    def __init__(self, con):
        self._con = con

    def load_rules(self, path: str) -> Rules:
        return load_rules(path)

    def apply_rules(self, rules: Rules) -> r.PrivacyOutcome:
        """Re-apply the CURRENT rules to every fact (used at the end of 04, 11 and the revision step, so a rebuild
        retroactively privatizes old facts). Raise-only: a fact stored 'private' stays private even if the rule
        that once caught it is gone. Also writes subjects.private (1 for every subject whose own tag or an
        ancestor's tag is private). Does not commit."""
        deps = privacy_floor_service.PrivacyFloorDeps(_FloorRows(self._con))
        return privacy_floor_service.apply_privacy_floor(deps, rules)

    def raise_history_of_private_facts(self) -> None:
        """Every revision snapshot of a private fact becomes private (history is never more visible than the
        fact); which facts those are is `fact_state.facts_with_private_history`."""
        visibilities = {i: v for i, v in self._con.execute("SELECT id, visibility FROM facts")}
        ids = fact_state.facts_with_private_history(visibilities)
        for start in range(0, len(ids), _CHUNK):
            chunk = ids[start:start + _CHUNK]
            self._con.execute("UPDATE fact_revisions SET visibility = 'private' WHERE visibility != 'private' "
                              f"AND fact_id IN ({', '.join('?' for _ in chunk)})", chunk)


def apply_rules_to_db(con, rules):
    """`SqlitePrivacyRepository.apply_rules` as the fact ingest and the tests read it:
    {"raised": [(fact_id, explanation)...], "private_subjects": n}. Does not commit."""
    outcome = SqlitePrivacyRepository(con).apply_rules(rules)
    return {"raised": list(outcome.raised), "private_subjects": outcome.private_subjects}


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
