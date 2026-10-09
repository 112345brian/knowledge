"""Persistent identifiers for sources (#48): DOI, ISBN, ISSN, PMID, arXiv and `other`, normalized so the same work
cannot hide behind two spellings. Library only; `knowledge.py source ids` and `search --identifier` use it.

`normalize(scheme, raw)` returns the canonical value or raises IdentifierError naming the scheme and the offending
value. It never touches the network and never "fixes" a value it cannot verify:
  doi    lowercased; `https://doi.org/`, `http://dx.doi.org/` and `doi:` prefixes removed; trailing punctuation
         (. , ; : ) ] } quotes) removed; must look like 10.NNNN/suffix
  isbn   hyphens and spaces removed, check digit VERIFIED, and stored as ISBN-13 (an ISBN-10 is converted), so
         the two forms of one book are the same identifier
  issn   NNNN-NNNN, check digit verified, upper-case X
  pmid   digits, no leading zeros
  arxiv  `arXiv:` / URL prefixes and a trailing version (v2) removed: 2101.00001 or hep-th/9901001
  other  free text with whitespace collapsed (a PMC id is stored as `pmcid:PMC123456`)
`candidates(raw)` is for lookups where the scheme is unknown: every (scheme, value) the text could be.
"""
import re

SCHEMES = ("doi", "isbn", "issn", "pmid", "arxiv", "other")  # keep in sync with the CHECK in schema.sql
_TRAILING = ".,;:)]}\"'>"


# Values people type when a source simply has no identifier. They mean "none", not "a malformed identifier", so the
# loaders skip them; every OTHER value that fails to normalize still fails the build.
PLACEHOLDERS = frozenset({"n/a", "na", "none", "null", "nil", "unknown", "tbd", "pending", "-", "--", "?", "no doi", "no pmid"})


def is_placeholder(value):
    return isinstance(value, str) and value.strip().casefold() in PLACEHOLDERS


class IdentifierError(ValueError):
    """An identifier is malformed or fails its checksum. The message names the scheme and the value."""


def _fail(scheme, raw, why):
    raise IdentifierError(f"{scheme} {raw!r}: {why}")


def _isbn13_check(digits12):
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits12))
    return str((10 - total % 10) % 10)


def normalize_doi(raw):
    v = raw.strip()
    v = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", v, flags=re.I)
    v = v.strip().rstrip(_TRAILING).lower()
    if not re.fullmatch(r"10\.\d{4,9}/\S+", v):
        _fail("doi", raw, "expected 10.NNNN/suffix")
    return v


def normalize_isbn(raw):
    v = re.sub(r"^isbn(?:-1[03])?:?\s*", "", raw.strip(), flags=re.I)
    v = re.sub(r"[\s-]", "", v).upper()
    if re.fullmatch(r"\d{9}[\dX]", v):
        total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(v))
        if total % 11 != 0:
            _fail("isbn", raw, "ISBN-10 check digit is wrong")
        body = "978" + v[:9]
        return body + _isbn13_check(body)
    if re.fullmatch(r"\d{13}", v):
        if v[:3] not in ("978", "979"):
            _fail("isbn", raw, "an ISBN-13 starts with 978 or 979")
        if _isbn13_check(v[:12]) != v[12]:
            _fail("isbn", raw, "ISBN-13 check digit is wrong")
        return v
    _fail("isbn", raw, "expected 10 or 13 digits (hyphens allowed)")


def normalize_issn(raw):
    v = re.sub(r"^issn:?\s*", "", raw.strip(), flags=re.I)
    v = re.sub(r"[\s-]", "", v).upper()
    if not re.fullmatch(r"\d{7}[\dX]", v):
        _fail("issn", raw, "expected NNNN-NNNN")
    total = sum((8 - i) * int(c) for i, c in enumerate(v[:7]))
    check = (11 - total % 11) % 11
    if v[7] != ("X" if check == 10 else str(check)):
        _fail("issn", raw, "check digit is wrong")
    return v[:4] + "-" + v[4:]


def normalize_pmid(raw):
    v = re.sub(r"^pmid:?\s*", "", raw.strip(), flags=re.I)
    if not re.fullmatch(r"[1-9]\d{0,8}", v):
        _fail("pmid", raw, "expected digits without leading zeros (at most 9)")
    return v


def normalize_arxiv(raw):
    v = re.sub(r"^(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv:\s*)", "", raw.strip(), flags=re.I)
    v = re.sub(r"(?:\.pdf)?$", "", v)
    v = re.sub(r"v\d+$", "", v).lower()
    if not (re.fullmatch(r"\d{4}\.\d{4,5}", v) or re.fullmatch(r"[a-z\-]+(?:\.[a-z]{2})?/\d{7}", v)):
        _fail("arxiv", raw, "expected NNNN.NNNNN or archive/NNNNNNN")
    return v


def normalize_other(raw):
    v = " ".join(raw.split())
    if not v:
        _fail("other", raw, "is blank")
    return v


_NORMALIZERS = {"doi": normalize_doi, "isbn": normalize_isbn, "issn": normalize_issn, "pmid": normalize_pmid,
                "arxiv": normalize_arxiv, "other": normalize_other}


def normalize(scheme, raw):
    if scheme not in _NORMALIZERS:
        raise IdentifierError(f"scheme {scheme!r} must be one of {list(SCHEMES)}")
    if not isinstance(raw, str):
        _fail(scheme, raw, "must be text")
    return _NORMALIZERS[scheme](raw)


def pmcid(raw):
    """A PubMed Central id as an `other` identifier value: `pmcid:PMC123456`."""
    v = re.sub(r"^pmcid:?\s*", "", raw.strip(), flags=re.I).upper()
    if not re.fullmatch(r"PMC\d{4,9}", v):
        _fail("other", raw, "expected a PMC id like PMC1234567")
    return "pmcid:" + v


def candidates(raw):
    """Every (scheme, value) that `raw` could be, for a lookup with no scheme given. An explicit `scheme:` prefix
    (doi:, isbn:, issn:, pmid:, arxiv:, pmcid:) restricts it. Never raises; [] when nothing fits."""
    if not isinstance(raw, str) or not raw.strip():
        return []
    text = raw.strip()
    m = re.match(r"^(doi|isbn|issn|pmid|arxiv|pmcid):", text, flags=re.I)
    out = []
    if m and m.group(1).lower() == "pmcid":
        try:
            return [("other", pmcid(text))]
        except IdentifierError:
            return []
    for scheme in ([m.group(1).lower()] if m else ["doi", "isbn", "issn", "pmid", "arxiv"]):
        try:
            out.append((scheme, normalize(scheme, text)))
        except IdentifierError:
            continue
    try:
        out.append(("other", pmcid(text)))
    except IdentifierError:
        pass
    if not m:
        out.append(("other", normalize_other(text)))
    return out


def filter_clause(raw, fact_alias="f"):
    """(sql, params) restricting `fact_alias` rows to facts that cite a source with the identifier `raw` (any scheme it
    could be, see `candidates`). Used by knowledge.py and modes.py so the two query layers cannot disagree.
    Raises ValueError when `raw` cannot be any identifier at all; an identifier nobody has matches no facts."""
    cands = candidates(raw)
    if not cands:
        raise ValueError(f"identifier {raw!r} is blank or not a recognizable DOI, ISBN, ISSN, PMID, arXiv or PMC id")
    pairs = " OR ".join("(si.scheme = ? AND si.value = ?)" for _ in cands)
    sql = (f" AND {fact_alias}.id IN (SELECT fs.fact_id FROM fact_sources fs "
           f"JOIN source_identifiers si ON si.source_id = fs.source_id WHERE {pairs})")
    return sql, [x for c in cands for x in c]
