"""Custodial history for sources and vault files (#46): when and how a file came into the collection.

Archival description records a custodial history. We have `url` and `retrieved_date` only when someone typed
them; macOS records the same information for free on downloaded files, as extended attributes:
  com.apple.metadata:kMDItemWhereFroms   a binary plist, usually [download URL, referrer URL]
  com.apple.quarantine                   "flags;<hex unix time>;<app>;<uuid>"; the time is when it was downloaded

Columns on `sources` and `vault_files`: acquired_at, acquired_via (download | manual | export | unknown),
where_from (a URL), acquired_note.

Precedence: values from the DATA come first (frontmatter keys `acquired-at`, `acquired-via`, `where-from`; the
same names with underscores in manual_sources.json). The file attributes only fill gaps, field by field, and
never overwrite anything the data has. A value that came from the attributes is marked: `acquired_note` says
so ("from macOS file attributes: where_from, acquired_at") and `acquired_via` is 'download' unless the data
set it. A source with no data and no attributes keeps NULLs; nothing is invented.

Reading is READ-ONLY and best effort: it uses the `xattr` command and plistlib (no new dependency), only on
macOS, and every failure (another platform, no `xattr`, a missing or unreadable file, a plist that does not
parse, a timeout) is a silent no-op that returns nothing. Nothing here ever writes an attribute.

where_from URLs can reveal private interests, so they (and every column here) stay out of the normal-only DB.
"""
import os
import plistlib
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import validtime

VIA_VALUES = ("download", "manual", "export", "unknown")  # keep in sync with the CHECKs in schema.sql
WHERE_FROMS = "com.apple.metadata:kMDItemWhereFroms"
QUARANTINE = "com.apple.quarantine"
ATTR_NOTE_PREFIX = "from macOS file attributes"
COLUMNS = ("acquired_at", "acquired_via", "where_from", "acquired_note")
_TIMEOUT = 5


class AcquisitionError(Exception):
    """A value in the data is invalid. The message names the source."""


def _text(value, where, field):
    if value is None:
        return None
    if not isinstance(value, str):
        raise AcquisitionError(f"{where}: {field} must be text, not {type(value).__name__}")
    return value.strip() or None


def _valid_when(value):
    if validtime.is_valid_boundary(value):
        return True
    try:
        datetime.fromisoformat(value)
        return True
    except ValueError:
        return False


def normalize_data(raw, where):
    """The data-supplied acquisition fields {acquired_at, acquired_via, where_from, acquired_note}: blank -> None,
    validated. Raises AcquisitionError naming `where`."""
    out = {k: _text(raw.get(k), where, k) for k in COLUMNS}
    if out["acquired_at"] is not None and not _valid_when(out["acquired_at"]):
        raise AcquisitionError(f"{where}: acquired_at {out['acquired_at']!r} must be an ISO date (YYYY, YYYY-MM, YYYY-MM-DD) or timestamp")
    if out["acquired_via"] is not None and out["acquired_via"] not in VIA_VALUES:
        raise AcquisitionError(f"{where}: acquired_via {out['acquired_via']!r} must be one of {list(VIA_VALUES)}")
    return out


# ------------------------------------------------------------------ reading the file attributes (read-only)

def _default_run(args):
    """Run `xattr` and return stdout text, or None on any failure."""
    try:
        # errors="replace": `xattr -p` prints some values as raw bytes; a decode error must not lose the other attribute
        proc = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=_TIMEOUT)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def available():
    return sys.platform == "darwin" and shutil.which("xattr") is not None


def _parse_where_froms(output):
    """The first URL in the kMDItemWhereFroms value (`xattr -px` prints the binary plist as a hex dump), or None."""
    try:
        blob = bytes.fromhex("".join(output.split()))
        urls = plistlib.loads(blob)
    except (ValueError, plistlib.InvalidFileException, OSError):
        return None
    if isinstance(urls, list):
        for u in urls:
            if isinstance(u, str) and u.strip():
                return u.strip()
    return None


def _parse_quarantine(output):
    """The download time (UTC ISO-8601 to the second) from "flags;hex time;app;uuid", or None."""
    m = re.match(r"^[0-9a-fA-F]+;([0-9a-fA-F]+);", output.strip())
    if not m:
        return None
    try:
        ts = datetime.fromtimestamp(int(m.group(1), 16), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    if not 1990 <= ts.year <= 2200:
        return None
    return ts.replace(microsecond=0).isoformat()


def read_attributes(path, run=None, platform_ok=None):
    """{'where_from': url, 'acquired_at': iso} for whatever the file's macOS attributes provide; {} elsewhere.
    `run` (args -> stdout or None) and `platform_ok` exist so tests can simulate the attributes on any platform."""
    if not path:
        return {}
    if platform_ok is None:
        platform_ok = available()
    if not platform_ok:
        return {}
    path = os.path.expanduser(path)
    run = run or _default_run
    names = run(["xattr", path])
    if not names:
        return {}
    have = set(names.split())
    out = {}
    if WHERE_FROMS in have:
        value = run(["xattr", "-px", WHERE_FROMS, path])      # -x: a hex dump, because the value is a binary plist
        url = _parse_where_froms(value) if value else None
        if url:
            out["where_from"] = url
    if QUARANTINE in have:
        value = run(["xattr", "-p", QUARANTINE, path])
        when = _parse_quarantine(value) if value else None
        if when:
            out["acquired_at"] = when
    return out


# ------------------------------------------------------------------ combining data and attributes

def resolve(data, path, run=None, platform_ok=None):
    """Final {acquired_at, acquired_via, where_from, acquired_note} for one file: `data` (already normalized,
    see normalize_data) wins field by field; the file attributes fill the gaps and are marked in acquired_note.
    With no data and no attributes everything stays None."""
    out = dict(data)
    attrs = read_attributes(path, run=run, platform_ok=platform_ok)
    used = [k for k in ("where_from", "acquired_at") if out.get(k) is None and attrs.get(k)]
    for k in used:
        out[k] = attrs[k]
    if used:
        if out.get("acquired_via") is None:
            out["acquired_via"] = "download"
        marker = f"{ATTR_NOTE_PREFIX}: {', '.join(used)}"
        out["acquired_note"] = f"{out['acquired_note']}; {marker}" if out.get("acquired_note") else marker
    return out
