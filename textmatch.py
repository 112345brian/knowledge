"""Literal whole-word term matching, shared by the privacy keyword list (#31) and entity aliases (#42).

One implementation so a keyword and an entity alias can never disagree about what "mentions" means.
Terms are normalized (NFC, whitespace collapsed, casefolded) and matched against NFC-normalized text as
WHOLE words: not preceded or followed by a letter, digit or underscore, so "brother" does not match
"brotherhood" but does match "brother's", "Brother," or "my-brother". Regex metacharacters in a term are
literal; spaces in a term match any whitespace run. No stemming, no fuzziness, no model.

Pure functions; no imports from this repo.
"""
import functools
import re
import unicodedata


def normalize_term(raw):
    """The canonical form of a term. Raises ValueError (clear message) for anything that could never
    match a whole word: non-string, blank, control characters, or no letters/digits at all."""
    if not isinstance(raw, str):
        raise ValueError(f"{raw!r} must be a string")
    term = " ".join(unicodedata.normalize("NFC", raw).split()).casefold()
    if not term:
        raise ValueError("is empty or blank")
    if any(unicodedata.category(c) in ("Cc", "Cf") for c in term):
        raise ValueError(f"{raw!r} contains control characters")
    if not re.search(r"\w", term):
        raise ValueError(f"{raw!r} has no letters or digits, so it can never match a whole word")
    return term


@functools.lru_cache(maxsize=512)
def _term_re(term):
    body = r"\s+".join(re.escape(part) for part in term.split(" "))
    return re.compile(r"(?<!\w)" + body + r"(?!\w)", re.IGNORECASE)


def find_terms(text, terms):
    """The `terms` (already normalized) found as whole words in `text`, in list order."""
    if not text:
        return []
    text = unicodedata.normalize("NFC", text)
    return [t for t in terms if _term_re(t).search(text)]
