"""One-time (but re-runnable/idempotent) cleanup of
the concerts export CSV (see paths.py -> CONCERTS_CSV):
  - strips stray whitespace from every field
  - drops exact duplicate rows (same name/date/location)
  - merges near-duplicates that differ only by location specificity
    (same name+date, one row's location is a prefix of the other's --
    keeps the more detailed location)

Rewrites the file in place. Safe to rerun: an already-clean file passes
through unchanged.
"""
import csv, os

from client import music_ingest_rules


CSV_PATH = None  # None: the configured concerts CSV, or None when unset; a caller or test may set a path

def _csv_path():
    if CSV_PATH is not None:
        return CSV_PATH
    import paths
    return os.path.expanduser(paths.settings().concerts_csv) if paths.settings().concerts_csv else None





def clean():
    with open(_csv_path(), encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    merged = music_ingest_rules.clean_concert_rows(rows)

    removed = len(rows) - len(merged)

    with open(_csv_path(), "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=music_ingest_rules.CONCERT_COLUMNS)
        writer.writeheader()
        writer.writerows(merged)

    print(f"[clean_concerts_csv] {len(rows)} rows -> {len(merged)} rows ({removed} duplicate/near-duplicate rows merged)")


if __name__ == "__main__":
    if _csv_path() is None:
        sys.exit("error: CONCERTS_CSV is not set in local_paths.py (the `concerts` client source is not configured)")
    clean()
