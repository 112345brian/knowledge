"""Valid time on facts (#40): when a fact was TRUE, as opposed to when it was recorded (date_added,
revisions: transaction time). Library only; no imports from this repo.

`valid_from` / `valid_to` are nullable ISO-8601 dates at three precisions: YYYY, YYYY-MM or YYYY-MM-DD.
  * valid_from NULL: no known start. valid_to NULL: "still true as far as known".
  * A partial date names the whole span: valid_from "2024" starts on 2024-01-01, valid_to "2024-03" ends on
    2024-03-31. A point-in-time fact sets both to the same value ("2024-03-15" is that one day; "2024-03" is
    all of March).
  * valid_to must not be before valid_from.
Ordering and containment are done on strings, by comparing a date to a boundary at the BOUNDARY's own
precision (the date's prefix of the boundary's length), which is correct for ISO strings and is exactly what
the SQL in knowledge.py / modes.py does, so Python and SQL cannot disagree.

A past valid_to does NOT change a fact's status: validity is not retraction, and the stale-premise audit
(claims_audit) treats it as information, never as invalidation.
"""
import calendar
import re

# [0-9], not \d: \d also matches non-ASCII digits (fullwidth, Arabic-Indic), which int() accepts and SQLite GLOB does not.
_RE = re.compile(r"^([0-9]{4})(?:-([0-9]{2})(?:-([0-9]{2}))?)?\Z")


def is_valid_boundary(value):
    """True for a real YYYY, YYYY-MM or YYYY-MM-DD (year 0001..9999; Feb 30 and month 13 are not real)."""
    if not isinstance(value, str):
        return False
    m = _RE.match(value)
    if not m:
        return False
    y = int(m.group(1))
    if y < 1:
        return False
    mo, d = m.group(2), m.group(3)
    if mo is not None and not 1 <= int(mo) <= 12:
        return False
    if d is not None and not 1 <= int(d) <= calendar.monthrange(y, int(mo))[1]:
        return False
    return True


def is_valid_date(value):
    """A full YYYY-MM-DD that exists (the form `--valid-at` takes)."""
    return is_valid_boundary(value) and len(value) == 10


def problems(valid_from, valid_to):
    """List of messages; empty when the pair is acceptable (both None is fine)."""
    out = []
    for name, v in (("valid_from", valid_from), ("valid_to", valid_to)):
        if v is not None and not is_valid_boundary(v):
            out.append(f"{name} {v!r} must be a real date: YYYY, YYYY-MM or YYYY-MM-DD")
    if not out and valid_from is not None and valid_to is not None:
        n = min(len(valid_from), len(valid_to))
        if valid_from[:n] > valid_to[:n]:
            out.append(f"valid_to {valid_to!r} is before valid_from {valid_from!r}")
    return out


def contains(valid_from, valid_to, at):
    """Whether `at` (a full YYYY-MM-DD) falls inside the interval. Raises ValueError for a bad `at` or bounds."""
    if not is_valid_date(at):
        raise ValueError(f"{at!r} must be a real date, YYYY-MM-DD")
    bad = problems(valid_from, valid_to)
    if bad:
        raise ValueError("; ".join(bad))
    if valid_from is not None and not valid_from <= at[:len(valid_from)]:
        return False
    if valid_to is not None and not at[:len(valid_to)] <= valid_to:
        return False
    return True


# The same test in SQL, for `col_from` / `col_to` columns and a bound parameter (:at or ?).
def sql_valid_at(col_from="f.valid_from", col_to="f.valid_to"):
    return (f"({col_from} IS NULL OR {col_from} <= substr(?, 1, length({col_from}))) "
            f"AND ({col_to} IS NULL OR substr(?, 1, length({col_to})) <= {col_to})")


def describe(valid_from, valid_to):
    """Short human form: '2024-03 .. (open)', '(unknown) .. 2024', a single value for a point in time, '' for none."""
    if valid_from is None and valid_to is None:
        return ""
    if valid_from is not None and valid_from == valid_to:
        return valid_from
    return f"{valid_from or '(unknown)'} .. {valid_to or '(open)'}"
