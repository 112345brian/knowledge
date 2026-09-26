#!/usr/bin/env python3
"""Append one ad hoc fact to general_facts.json (in knowledge-private, not
this repo), enforcing the shape 11_seed_general_facts.py expects (valid
trust_level, kebab-case subject name, a real source citekey if one is
given). This is the sanctioned way to add a "raw", not-project-specific
fact -- one with no vault or bodybuilding project behind it -- without
hand-editing the JSON or the live db.

Doesn't touch knowledge.db itself: rerun `python3 build.py` afterward to
pick the new fact up, same as any other data/*.json change.

Usage:
    python3 add_fact.py "Statement text." --subject some-subject --trust medium
    python3 add_fact.py "..." --subject x --trust high --domain general \
        --notes "..." --recheck-by 2026-12-01 --recheck-rationale "..." \
        --source-citekey some-existing-citekey --source-locator "p. 4"
"""
import argparse, json, os, re, sqlite3, sys

from paths import KNOWLEDGE_DB_DIR, PRIVATE_DATA_DIR

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(PRIVATE_DATA_DIR, "general_facts.json")
DB_PATH = os.path.join(os.path.expanduser(KNOWLEDGE_DB_DIR), "knowledge.db")
VALID_TRUST = {"verified", "high", "medium", "low", "unverified", "disputed"}
SUBJECT_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("statement", help="The fact itself, as a full sentence.")
    p.add_argument("--subject", required=True, help="kebab-case subject name, e.g. 'car-maintenance'. Reused if it already exists, otherwise created.")
    p.add_argument("--trust", required=True, choices=sorted(VALID_TRUST), dest="trust_level")
    p.add_argument("--domain", default="general", help="Only applies if --subject doesn't exist yet (default: general).")
    p.add_argument("--original-claim", action="store_true", dest="is_original_claim", help="This is your own conclusion, not something a source states.")
    p.add_argument("--not-personal", action="store_false", dest="is_personal", help="Mark as not about the user personally (default: personal).")
    p.add_argument("--trust-rationale")
    p.add_argument("--notes")
    p.add_argument("--recheck-by", help="A date (YYYY-MM-DD) or short phrase like 'next physical'.")
    p.add_argument("--recheck-rationale")
    p.add_argument("--source-citekey", help="Must already exist in the `sources` table.")
    p.add_argument("--source-locator")
    p.add_argument("--source-quote")
    p.set_defaults(is_personal=True)
    return p.parse_args(argv)


def validate(args):
    errors = []
    if not args.statement.strip():
        errors.append("statement is empty")
    if not SUBJECT_RE.match(args.subject):
        errors.append(f"--subject {args.subject!r} must be lowercase kebab-case (e.g. 'car-maintenance')")
    if args.recheck_by and DATE_RE.match(args.recheck_by) is None and len(args.recheck_by) < 4:
        errors.append(f"--recheck-by {args.recheck_by!r} looks too short to be a date or phrase")
    if (args.source_locator or args.source_quote) and not args.source_citekey:
        errors.append("--source-locator/--source-quote given without --source-citekey")
    if args.source_citekey:
        if not os.path.exists(DB_PATH):
            print(f"  (skipping citekey check -- {DB_PATH} doesn't exist yet)", file=sys.stderr)
        else:
            con = sqlite3.connect(DB_PATH)
            row = con.execute("SELECT 1 FROM sources WHERE citekey = ?", (args.source_citekey,)).fetchone()
            con.close()
            if not row:
                errors.append(f"--source-citekey {args.source_citekey!r} not found in sources table")
    return errors


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    errors = validate(args)
    if errors:
        for e in errors:
            print(f"error: {e}", file=sys.stderr)
        return 1

    entry = {
        "subject": args.subject,
        "statement": args.statement.strip(),
        "trust_level": args.trust_level,
        "is_original_claim": bool(args.is_original_claim),
        "is_personal": bool(args.is_personal),
    }
    if args.domain != "general":
        entry["domain"] = args.domain
    if args.trust_rationale:
        entry["trust_rationale"] = args.trust_rationale
    if args.notes:
        entry["notes"] = args.notes
    if args.recheck_by:
        entry["recheck_by"] = args.recheck_by
    if args.recheck_rationale:
        entry["recheck_rationale"] = args.recheck_rationale
    if args.source_citekey:
        entry["source_citekey"] = args.source_citekey
    if args.source_locator:
        entry["source_locator"] = args.source_locator
    if args.source_quote:
        entry["source_quote"] = args.source_quote

    items = json.load(open(DATA_PATH)) if os.path.exists(DATA_PATH) else []
    items.append(entry)
    with open(DATA_PATH, "w") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Added to {os.path.relpath(DATA_PATH, HERE)} ({len(items)} facts total). Run `python3 build.py` to rebuild knowledge.db.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
