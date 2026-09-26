"""One-time (but re-runnable/idempotent) cleanup of
<CONCERTS_CSV>:
  - strips stray whitespace from every field
  - drops exact duplicate rows (same name/date/location)
  - merges near-duplicates that differ only by location specificity
    (same name+date, one row's location is a prefix of the other's --
    keeps the more detailed location)

Rewrites the file in place. Safe to rerun: an already-clean file passes
through unchanged.
"""
import csv, os

from paths import CONCERTS_CSV

CSV_PATH = os.path.expanduser(CONCERTS_CSV)


def clean():
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    cleaned = []
    for r in rows:
        c = {k: v.strip() for k, v in r.items()}
        # "NO VALUES" is the actual name of a real punk festival (Goldenvoice's
        # "No Values"), not a placeholder -- just normalize the casing.
        if c["Concert"] == "NO VALUES Festival":
            c["Concert"] = "No Values"
        if c["Location"] == "NO VALUES":
            c["Location"] = "No Values"
        cleaned.append(c)

    # merge near-duplicates: same (name, start_date), one location a prefix of the other
    merged = []
    for r in cleaned:
        match = next((m for m in merged
                      if m["Concert"] == r["Concert"] and m["Start Date"] == r["Start Date"]
                      and (m["Location"].startswith(r["Location"]) or r["Location"].startswith(m["Location"]))),
                     None)
        if match:
            if len(r["Location"]) > len(match["Location"]):
                match["Location"] = r["Location"]
            if not match["Notes"] and r["Notes"]:
                match["Notes"] = r["Notes"]
            if not match["End Date"] and r["End Date"]:
                match["End Date"] = r["End Date"]
        else:
            merged.append(dict(r))

    removed = len(cleaned) - len(merged)

    with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["Concert", "Start Date", "End Date", "Location", "Notes"])
        writer.writeheader()
        writer.writerows(merged)

    print(f"[clean_concerts_csv] {len(rows)} rows -> {len(merged)} rows ({removed} duplicate/near-duplicate rows merged)")


if __name__ == "__main__":
    clean()
