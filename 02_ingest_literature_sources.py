"""Parse every citation note's (sources/*.md, see paths.py -> BODYBUILDING_VAULT)
frontmatter into a `sources` row. Mechanical: structured frontmatter -> low
risk of misreading. Run after 01_seed_sources.py.
"""
import sqlite3, os, re, glob, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _shared import link_authors, get_or_create_publisher
from paths import BODYBUILDING_VAULT as VAULT

SRC_DIR = os.path.expanduser(f"{VAULT}/sources")

TYPE_MAP = {
    "peer-reviewed-study": "primary", "primary": "primary", "primary-research": "primary",
    "case-report": "primary", "case-series": "primary", "conference-abstract": "primary",
    "preprint": "primary", "government-document": "primary", "drug-label": "primary",
    "statute": "primary",
    "secondary-reference": "secondary", "systematic-review": "secondary",
    "peer-reviewed-review": "secondary", "textbook-reference": "secondary",
    "review": "secondary", "clinical-guideline": "secondary", "professional-guidance": "secondary",
    "peer-reviewed-commentary": "secondary", "secondary-compilation": "secondary",
    "news-report": "secondary",
    "vendor-description": "tertiary", "web-reference": "tertiary",
    "search-methodology-note": "tertiary", "software": "tertiary",
}


def parse_frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}, text
    fm_text, body = m.group(1), text[m.end():]
    fields, lines, i = {}, fm_text.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        km = re.match(r'^([a-zA-Z0-9_-]+):\s*(.*)$', line)
        if not km:
            i += 1
            continue
        key, val = km.group(1), km.group(2).strip()
        if val == "":
            items, j = [], i + 1
            while j < len(lines) and re.match(r'^\s+-\s+', lines[j]):
                item = re.sub(r'^\s+-\s+', '', lines[j]).strip().strip('"').strip("'")
                item = re.sub(r'^\[\[|\]\]$', '', item)
                items.append(item)
                j += 1
            if items:
                fields[key] = items
                i = j
                continue
            fields[key] = None
            i += 1
        else:
            fields[key] = val.strip('"').strip("'")
            i += 1
    return fields, body


def first_quote(body, maxlen=300):
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith(">") and len(line) > 3:
            q = line.lstrip("> ").strip()
            if q.startswith("[!"):
                continue
            return q[:maxlen]
    return None


def parse_all():
    rows = []
    for fp in sorted(glob.glob(os.path.join(SRC_DIR, "*.md"))):
        base = os.path.basename(fp)
        if base == "README.md":
            continue
        text = open(fp, encoding="utf-8").read()
        fm, body = parse_frontmatter(text)

        title = fm.get("title")
        authors = fm.get("authors") or fm.get("author")
        author_str = "; ".join(authors) if isinstance(authors, list) else authors

        year = fm.get("year")
        journal = fm.get("journal") or fm.get("publisher")
        url = fm.get("url")
        doi, pmid = fm.get("doi"), fm.get("pmid")
        stype_raw = fm.get("source-type")
        stype = TYPE_MAP.get(stype_raw, "primary" if stype_raw else None)

        if not stype:
            low = base.lower()
            if any(k in low for k in ("meta-analysis", "review", "editorial", "commentary", "textbook")):
                stype = "secondary"
            elif any(k in low for k in ("label", "vendor", "web-")):
                stype = "tertiary"
            else:
                stype = "primary"

        citekey = fm.get("citekey", base.replace(".md", ""))
        name = title or citekey
        if author_str and year and title:
            first_author = author_str.split(";")[0].split(",")[0].strip()
            name = f"{first_author} et al. {year}, \"{title}\""
        elif author_str and year:
            first_author = author_str.split(";")[0].split(",")[0].strip()
            name = f"{first_author} et al. {year} ({citekey})"

        quote = first_quote(body)
        domain = fm.get("domain")
        desc_parts = []
        if domain:
            desc_parts.append(f"domain: {domain}")
        if doi:
            desc_parts.append(f"doi: {doi}")
        if pmid:
            desc_parts.append(f"pmid: {pmid}")
        if quote:
            desc_parts.append(f'quoted: "{quote}"')
        description = " | ".join(desc_parts)

        rows.append(dict(
            citekey=citekey, name=name, source_type=stype, author=author_str,
            publisher=journal, url=url, published_date=year, description=description,
            origin_path=f"{VAULT}/sources/{base}",
        ))
    return rows


def run(con):
    rows = parse_all()
    cur = con.cursor()
    existing = {r[0] for r in cur.execute("SELECT citekey FROM sources WHERE citekey IS NOT NULL")}
    inserted = 0
    for r in rows:
        if r["citekey"] in existing:
            continue
        row = {k: v for k, v in r.items() if k not in ("author", "publisher")}
        row["publisher_id"] = get_or_create_publisher(cur, r.get("publisher"))
        cur.execute(
            """INSERT INTO sources (citekey, name, source_type, publisher_id, url, published_date, retrieved_date, description, origin_path)
               VALUES (:citekey, :name, :source_type, :publisher_id, :url, :published_date, '2026-09-11', :description, :origin_path)""",
            row,
        )
        link_authors(cur, cur.lastrowid, r["author"])
        existing.add(r["citekey"])
        inserted += 1
    con.commit()
    print(f"[02_ingest_literature_sources] parsed {len(rows)} files, inserted {inserted} new sources")


if __name__ == "__main__":
    con = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.db"))
    con.execute("PRAGMA foreign_keys = ON;")
    run(con)
    con.close()
