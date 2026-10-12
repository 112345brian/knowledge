"""Identity rules the build shares, pure: how names and keys are normalized before they are compared.

Domain service ring: no I/O. The byline split and the identifier warning are defined here. Optional sources keep
their own name canonicalization and stateful lookup rules in `client/`.
"""
from typing import List, Optional, Tuple


def author_links(author_string: Optional[str]) -> List[Tuple[str, int]]:
    """[(author name, byline position)] for a '; '-joined byline. Names are stripped and blanks skipped, but a
    blank still occupies a position, so `"A; ; B"` gives [("A", 0), ("B", 2)]. Falsy input gives []."""
    if not author_string:
        return []
    return [(name, i) for i, name in enumerate(a.strip() for a in author_string.split(";")) if name]


def identifier_conflict_warning(scheme: str, value: str, owner_citekey: Optional[str], citekey: Optional[str]) -> str:
    """The build's one line about an identifier two sources claim."""
    return (f"  WARNING -- duplicate identifier {scheme}:{value} shared by sources {owner_citekey!r} "
            f"and {citekey!r} (not merged)")
