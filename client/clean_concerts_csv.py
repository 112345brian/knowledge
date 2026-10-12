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
import csv
import io
import sys

from client import music_ingest_rules


def clean(files, csv_path, stdout=None):
    """Clean the CSV at `csv_path` through `files` (a DataFiles); the summary line goes to `stdout` (default stdout)."""
    rows = list(csv.DictReader(io.StringIO(files.read_text(csv_path, "utf-8-sig", newline=""), newline="")))

    merged = music_ingest_rules.clean_concert_rows(rows)

    removed = len(rows) - len(merged)

    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=music_ingest_rules.CONCERT_COLUMNS)
    writer.writeheader()
    writer.writerows(merged)
    files.write_text(csv_path, out.getvalue(), "utf-8", newline="")

    print(f"[clean_concerts_csv] {len(rows)} rows -> {len(merged)} rows ({removed} duplicate/near-duplicate rows merged)",
          file=stdout)


def main():
    from container import Container
    app = Container()
    csv_path = app.locations.client_input("CONCERTS_CSV")
    if csv_path is None:
        sys.exit("error: CONCERTS_CSV is not set in local_paths.py (the `concerts` client source is not configured)")
    clean(app.datafiles, csv_path)


if __name__ == "__main__":
    main()
