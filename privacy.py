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

This module is the domain: pure functions only, no file, database or git access (enforced by the
import-linter contract "domain-has-no-infrastructure"). Reading and writing the rules file and
applying rules to a db live in the adapter `privacy_store`. The edit helpers (tag_subject,
untag_subject, add_keyword, remove_keyword) return new rules and never write; the CLI saves them
through privacy_store and wraps that with private_git (#10).
"""
import re
from dataclasses import dataclass, field, replace
from typing import Dict, FrozenSet, List, Optional, Tuple

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
    # Context a caller attaches for resolution (not stored in the rules file):
    parents: Dict[str, Optional[str]] = field(default_factory=dict)  # subject name -> parent name
    known_subjects: Optional[FrozenSet[str]] = None  # None = do not enforce the unknown-subject rule
    # Private entities (#42), read from entities.json next to the rules file: ((canonical_name, terms), ...).
    # Not part of the rules file itself; save_rules never writes them. Last, so the positional order of the
    # fields that existed before (tags, keywords, parents, known_subjects) is unchanged.
    entities: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()

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


def subject_chain(subject, rules):
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
        chain, cyclic = subject_chain(subject, rules)
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
