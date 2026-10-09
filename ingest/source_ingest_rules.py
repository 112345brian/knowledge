"""Source ingest rules (steps 01 and 02), domain: no file, no db.

Parsing a citation note's frontmatter into a `sources` row, the source-type mapping, the quote shown in the
description, the byline split, and the placeholder substitution for the hand-authored sources. The scripts
read the notes and the JSON and write the rows; they call these.
"""
import re

import source_status

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


def note_to_source_row(base, text, vault):
    """The `sources` row (citekey, name, source_type, author, publisher, url, published_date, description,
    origin_path) for the citation note `base` (its file name) whose text is `text`."""
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

    where = f"{base}: source {citekey!r}"
    status_fields = source_status.normalize_fields({
        "status": fm.get("source-status"), "status_date": fm.get("source-status-date"), "status_note": fm.get("source-status-note"),
        "edition": fm.get("edition"), "original_published_date": fm.get("original-published-date"),
    }, where)
    relations = [(citekey, rel, target) for rel in ("replaces", "is-version-of")
                 for target in source_status.as_list(fm.get(rel))]

    return dict(
        citekey=citekey, relations=relations, **status_fields,
        # raw inputs for the adapter: identifiers are checked there, the custodial fields are resolved against the file
        raw_identifiers={k: fm.get(k) for k in ("doi", "pmid", "pmcid", "isbn", "issn", "arxiv")},
        raw_acquired={"acquired_at": fm.get("acquired-at"), "acquired_via": fm.get("acquired-via"),
                      "where_from": fm.get("where-from"), "acquired_note": fm.get("acquired-note")},
        where=where, name=name, source_type=stype, author=author_str,
        publisher=journal, url=url, published_date=year, description=description,
        origin_path=f"{vault}/sources/{base}",
    )


def split_authors(author_string):
    """The names of a '; '-joined byline, in order, blanks dropped. Falsy input gives []."""
    if not author_string:
        return []
    return [name for name in (a.strip() for a in author_string.split(";")) if name]


def format_origin_path(origin_path, vault, health):
    """Substitute the {VAULT} and {HEALTH} placeholders of a manual source's origin_path."""
    return origin_path.format(VAULT=vault, HEALTH=health) if origin_path else origin_path
