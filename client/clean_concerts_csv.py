"""Idempotent cleanup of the concerts CSV (concerts source).

One-time (but re-runnable/idempotent) cleanup of
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


def clean(csv_path):
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    merged = music_ingest_rules.clean_concert_rows(rows)

    removed = len(rows) - len(merged)

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=music_ingest_rules.CONCERT_COLUMNS)
        writer.writeheader()
        writer.writerows(merged)

    print(f"[clean_concerts_csv] {len(rows)} rows -> {len(merged)} rows ({removed} duplicate/near-duplicate rows merged)")


if __name__ == "__main__":
    import paths
    csv_path = paths.load().concerts_csv
    if csv_path is None:
        sys.exit("error: CONCERTS_CSV is not set in local_paths.py (the `concerts` client source is not configured)")
    clean(csv_path)
