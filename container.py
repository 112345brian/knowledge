"""The composition root of the whole application: one object that wires every use case for one `Locations`.

An entry point (the Typer app, the build script, an embedding program, a test) makes one `Container` and
asks it for what it needs. Nothing is built at import and nothing is global: two containers with different
locations are independent, and a container made without locations reads them (`paths.load()`) the first time
a use case is asked for.
"""
from functools import cached_property

import add_fact
import entities
import facts_batch
import lifecycle
import migrate_memory
import paths
import queries
import review
import rules_edit
import scripts
import subjects


class Container:
    def __init__(self, locations=None):
        self._locations = locations

    @cached_property
    def locations(self):
        return self._locations if self._locations is not None else paths.load()

    @cached_property
    def review(self):
        return review.compose(self.locations)

    @cached_property
    def lifecycle(self):
        return lifecycle.compose(self.locations, self.review)

    @cached_property
    def add_fact(self):
        return add_fact.compose(self.locations)

    @cached_property
    def facts_batch(self):
        return facts_batch.compose(self.locations, self.add_fact)

    @cached_property
    def migrate_memory(self):
        return migrate_memory.compose(self.locations, self.add_fact)

    @cached_property
    def rules_edit(self):
        return rules_edit.compose(self.locations)

    @cached_property
    def entities(self):
        return entities.compose(self.locations)

    @cached_property
    def subjects(self):
        return subjects.compose(self.locations)

    @cached_property
    def queries(self):
        return queries.compose(self.locations)

    @cached_property
    def scripts(self):
        return scripts.compose()
