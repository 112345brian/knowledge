"""Deterministic privacy rules (issue #31). The model never decides how private a
fact is; this module computes it, and a caller's request can only raise it.

    stored visibility = the most restrictive of
      1. what the caller asked for ('private' or 'normal'),
      2. the tag on the fact's subject, or on any ancestor (subjects form a tree via parent_id),
      3. a keyword/name list matched on WHOLE words, case-insensitively,
      4. a private entity (entities.json, #42) whose name or alias appears as a whole word.
    An unknown subject is private (fail closed).

This is word matching plus subject tags. It is NOT semantic detection: nicknames, pronouns and
unlisted names are missed, which is why human review stays in the loop.

The rules live in `privacy_rules.json` in PRIVATE_DATA_DIR (knowledge-private), because they hold
personal names. This public repo never contains that file. Format:

    {"version": 1,
     "subject_tags": {"some-subject": "private", "other-subject": "normal"},
     "keywords": ["some name", "another"]}

`"normal"` on a subject only registers it as known (so the unknown-subject rule does not fire); it
never lowers anything: a private parent or a keyword hit still wins.

No rules file: no tags, no keywords (empty rules). That is deterministic and fails closed where it
matters: callers that know the subject list still get 'private' for unknown subjects.

Pure functions: resolve_visibility / check never touch the disk. load_rules/save_rules and the
edit helpers (tag_subject, untag_subject, add_keyword, remove_keyword) only touch the rules file
you pass. They do not commit; the CLI wraps them with private_git (#10).
"""
import contextlib
import json
import os
import re
import sqlite3
import stat
import uuid
from dataclasses import dataclass, field, replace
from typing import Dict, FrozenSet, List, Optional, Tuple

import entities as entities_lib
import textmatch

RULES_FILENAME = "privacy_rules.json"
VERSION = 1
VALID_VISIBILITY = ("private", "normal")  # keep in sync with the CHECK on facts.visibility
VALID_TAGS = ("private", "normal")
_SUBJECT_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_TOP_KEYS = {"version", "subject_tags", "keywords"}


class PrivacyRulesError(Exception):
    """The rules file or an edit to it is invalid. Always a clear message; never a guess."""


@dataclass(frozen=True)
class Rules:
    subject_tags: Dict[str, str] = field(default_factory=dict)
    keywords: Tuple[str, ...] = ()
    # Private entities (#42), read from entities.json next to the rules file: ((canonical_name, terms), ...).
    # Not part of the rules file itself; save_rules never writes them.
    entities: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()
    # Context a caller attaches for resolution (not stored in the rules file):
    parents: Dict[str, Optional[str]] = field(default_factory=dict)  # subject name -> parent name
    known_subjects: Optional[FrozenSet[str]] = None  # None = do not enforce the unknown-subject rule

    def with_context(self, parents=None, known_subjects=None):
        return replace(self,
                       parents=dict(parents) if parents is not None else self.parents,
                       known_subjects=frozenset(known_subjects) if known_subjects is not None else self.known_subjects)


@dataclass(frozen=True)
class Reason:
    kind: str    # 'requested' | 'subject-tag' | 'keyword' | 'entity' | 'unknown-subject' | 'cycle'
    detail: str
    forces_private: bool

    def __str__(self):
        return f"{self.kind}: {self.detail}"


@dataclass(frozen=True)
class Resolution:
    visibility: str
    reasons: Tuple[Reason, ...] = ()

    @property
    def raised_by(self) -> List[Reason]:
        """The rules that forced 'private' (empty when the result is 'normal')."""
        return [r for r in self.reasons if r.forces_private]

    @property
    def raised_above_request(self) -> bool:
        return self.visibility == "private" and not any(r.kind == "requested" for r in self.reasons)

    def explain(self) -> str:
        if self.visibility == "normal":
            return "normal: no privacy rule applies"
        return "private: " + "; ".join(str(r) for r in self.raised_by)


# ----------------------------------------------------------------- loading / saving

def rules_path(data_dir=None):
    if data_dir is None:
        from paths import PRIVATE_DATA_DIR  # lazy: keeps this module importable without a private repo
        data_dir = PRIVATE_DATA_DIR
    return os.path.join(data_dir, RULES_FILENAME)


def _normalize_keyword(raw):
    try:
        return textmatch.normalize_term(raw)
    except ValueError as e:
        raise PrivacyRulesError(f"keyword {e}") from None


def _validate_subject(name):
    if not isinstance(name, str) or not _SUBJECT_RE.match(name):
        raise PrivacyRulesError(f"subject {name!r} must be lowercase kebab-case (e.g. 'family')")
    return name


def parse_rules(data, source="privacy rules"):
    if not isinstance(data, dict):
        raise PrivacyRulesError(f"{source} must be a JSON object, got {type(data).__name__}")
    extra = set(data) - _TOP_KEYS
    if extra:
        raise PrivacyRulesError(f"{source} has unknown keys {sorted(extra)}; allowed: {sorted(_TOP_KEYS)}")
    if data.get("version", VERSION) != VERSION:
        raise PrivacyRulesError(f"{source} has version {data.get('version')!r}; this code reads version {VERSION}")
    tags = data.get("subject_tags", {})
    if not isinstance(tags, dict):
        raise PrivacyRulesError(f"{source}: subject_tags must be an object of subject -> tag")
    for name, tag in tags.items():
        _validate_subject(name)
        if tag not in VALID_TAGS:
            raise PrivacyRulesError(f"{source}: subject {name!r} has tag {tag!r}; must be one of {list(VALID_TAGS)}")
    kws = data.get("keywords", [])
    if not isinstance(kws, list):
        raise PrivacyRulesError(f"{source}: keywords must be a list of strings")
    seen = []
    for k in kws:
        n = _normalize_keyword(k)
        if n not in seen:
            seen.append(n)
    return Rules(subject_tags=dict(tags), keywords=tuple(seen))


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


# ----------------------------------------------------------------- edits (library for the CLI)
# Each returns (new_rules, changed). They never write; the caller save_rules()s.

def tag_subject(rules, subject, tag="private"):
    _validate_subject(subject)
    if tag not in VALID_TAGS:
        raise PrivacyRulesError(f"tag {tag!r} must be one of {list(VALID_TAGS)}")
    if rules.subject_tags.get(subject) == tag:
        return rules, False
    return replace(rules, subject_tags={**rules.subject_tags, subject: tag}), True


def untag_subject(rules, subject):
    if subject not in rules.subject_tags:
        return rules, False
    return replace(rules, subject_tags={k: v for k, v in rules.subject_tags.items() if k != subject}), True


def add_keyword(rules, keyword):
    kw = _normalize_keyword(keyword)
    if kw in rules.keywords:
        return rules, False
    return replace(rules, keywords=rules.keywords + (kw,)), True


def remove_keyword(rules, keyword):
    kw = _normalize_keyword(keyword)
    if kw not in rules.keywords:
        return rules, False
    return replace(rules, keywords=tuple(k for k in rules.keywords if k != kw)), True


# ----------------------------------------------------------------- the resolver

def match_keywords(statement, keywords):
    """The listed keywords found as whole words in `statement`, in list order (textmatch.find_terms)."""
    return textmatch.find_terms(statement, keywords)


def _subject_chain(subject, rules):
    """(subject, parent, grandparent, ...) by name, plus whether a cycle was hit."""
    chain, seen = [], set()
    cur = subject
    while cur is not None:
        if cur in seen:
            return chain, True
        seen.add(cur)
        chain.append(cur)
        cur = rules.parents.get(cur)
    return chain, False


def resolve_visibility(subject, statement, requested, rules, extra_text=None) -> Resolution:
    """Pure. The stored visibility and which rules raised it. `requested` must be 'private' or
    'normal' (anything else is a ValueError: a caller bug must not be guessed at). The result is
    'private' if any input is private, so no input can lower it.

    `extra_text` (optional iterable of strings): the fact's OTHER free-text fields (notes, trust and
    recheck rationale, source quote, citation locator/quote). The keyword list is matched against
    them too, because they are stored, searchable and (in some tiers) served just like the statement:
    a listed name in `notes` must make the fact private even when the statement is harmless."""
    if not isinstance(requested, str) or requested not in VALID_VISIBILITY:
        raise ValueError(f"requested visibility {requested!r} must be one of {list(VALID_VISIBILITY)}")
    reasons = []
    if requested == "private":
        reasons.append(Reason("requested", "the caller asked for private", True))

    if not isinstance(subject, str) or not subject.strip():
        reasons.append(Reason("unknown-subject", "no subject given (fail closed)", True))
    else:
        subject = subject.strip()
        chain, cyclic = _subject_chain(subject, rules)
        if cyclic:
            reasons.append(Reason("cycle", f"the subject tree above {subject!r} loops (fail closed)", True))
        for name in chain:
            if rules.subject_tags.get(name) == "private":
                detail = f"subject {subject!r} is tagged private" if name == subject else \
                    f"subject {subject!r} is under {name!r}, which is tagged private"
                reasons.append(Reason("subject-tag", detail, True))
                break
        known = rules.known_subjects
        registered = subject in rules.subject_tags
        if known is not None and subject not in known and not registered:
            reasons.append(Reason("unknown-subject", f"subject {subject!r} does not exist yet (fail closed)", True))

    in_statement = match_keywords(statement if isinstance(statement, str) else "", rules.keywords)
    for kw in in_statement:
        reasons.append(Reason("keyword", f"the statement contains the listed word {kw!r}", True))
    if extra_text:
        other = "\n".join(t for t in extra_text if isinstance(t, str) and t)
        for kw in match_keywords(other, rules.keywords):
            if kw not in in_statement:
                reasons.append(Reason("keyword", f"another field of the fact (notes, rationale, quote or citation) "
                                                 f"contains the listed word {kw!r}", True))

    other_text = "\n".join(t for t in (extra_text or ()) if isinstance(t, str) and t)
    for name, name_terms in rules.entities:
        if textmatch.find_terms(statement if isinstance(statement, str) else "", name_terms):
            reasons.append(Reason("entity", f"the statement mentions the private entity {name!r}", True))
        elif textmatch.find_terms(other_text, name_terms):
            reasons.append(Reason("entity", f"another field of the fact (notes, rationale, quote or citation) "
                                            f"mentions the private entity {name!r}", True))

    visibility = "private" if any(r.forces_private for r in reasons) else "normal"
    return Resolution(visibility, tuple(reasons))


def check(subject, statement, rules, requested="normal") -> Resolution:
    """What `knowledge.py privacy check` prints: resolve as if a caller asked for `requested`
    (default 'normal', so the answer shows what the rules alone would do)."""
    return resolve_visibility(subject, statement, requested, rules)


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
        chain, cyclic = _subject_chain(name, ctx)
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
