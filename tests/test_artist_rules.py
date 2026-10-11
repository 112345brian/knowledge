"""The artist-name rules (`client.artist_rules`): canonical spelling and the case-insensitive key."""
from client.artist_rules import artist_key, canonical_artist_name


def test_canonical_name_unescapes_html_before_matching_aliases():
    assert canonical_artist_name("Simon &amp; Garfunkel") == "Simon & Garfunkel"
    assert canonical_artist_name("A &amp; B", {"a & b": "A and B"}) == "A and B"


def test_aliases_are_keyed_by_the_lowercased_unescaped_name_and_miss_leaves_the_name_alone():
    aliases = {"jpegmafia": "JPEGMAFIA"}
    assert canonical_artist_name("Jpegmafia", aliases) == "JPEGMAFIA"
    assert canonical_artist_name("Other", aliases) == "Other"
    assert canonical_artist_name("Other", None) == "Other"


def test_canonical_name_edge_inputs():
    assert canonical_artist_name("") == ""
    assert canonical_artist_name("  Padded  ") == "  Padded  "      # no stripping: callers own that
    assert canonical_artist_name("A\x00B") == "A\x00B"


def test_key_is_python_lower_not_ascii_only_folding():
    assert artist_key("JPEGMAFIA") == artist_key("Jpegmafia") == "jpegmafia"
    assert artist_key("ÉMILE") == "émile"
    assert artist_key("") == ""
