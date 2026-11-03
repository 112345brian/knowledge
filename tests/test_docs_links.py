"""The documentation hangs together: every relative link resolves, and docs/README.md indexes every document.

The generated regions are checked by `knowledge.py docs check` (tests/test_docs.py); this checks the hand-written parts
nobody regenerates, which is where a moved or new file leaves a dead link or an unlisted document.
"""
import glob
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")


def _documents():
    return ["README.md", "CHANGELOG.md", *sorted(glob.glob("docs/*.md")), *sorted(glob.glob("skills/*/SKILL.md"))]


def test_every_relative_link_in_the_readme_and_docs_resolves():
    broken = []
    for doc in _documents():
        base = os.path.dirname(os.path.join(REPO, doc))
        with open(os.path.join(REPO, doc), encoding="utf-8") as f:
            for target in LINK.findall(f.read()):
                if re.match(r"[a-z][a-z0-9+.-]*:", target):
                    continue                                     # https://..., mailto:
                if not os.path.exists(os.path.normpath(os.path.join(base, target))):
                    broken.append(f"{doc} -> {target}")
    assert broken == [], broken


def test_the_docs_index_lists_every_document():
    with open(os.path.join(REPO, "docs", "README.md"), encoding="utf-8") as f:
        index = f.read()
    unlisted = [os.path.basename(p) for p in sorted(glob.glob(os.path.join(REPO, "docs", "*.md")))
                if os.path.basename(p) != "README.md" and f"({os.path.basename(p)})" not in index]
    assert unlisted == [], f"add to docs/README.md: {unlisted}"
