"""The entity vocabulary and its error type: what the entities file may contain, free of how it is stored.

Model ring: imports nothing else in the repo. `entities_store` (the file adapter) raises `EntitiesError` and
validates against `ENTITY_TYPES`; the CLI imports them from here instead of reaching through a facade into
infrastructure.
"""

ENTITY_TYPES = ("person", "organization", "place", "project", "substance", "other")  # keep in sync with schema.sql


class EntitiesError(Exception):
    """entities.json is unreadable or breaks a rule. The message is safe to show as is."""
