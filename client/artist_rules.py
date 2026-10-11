"""Pure helpers for canonicalizing names from optional music sources.

The definitions live in `identity_rules` (the one place artist identity is defined); the music steps that still
import this module get the same functions.
"""
from identity_rules import artist_key, canonical_artist_name  # noqa: F401  (re-exported)
