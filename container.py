"""The composition root of the whole application: one object that wires every use case for one `Locations` and one `Clock`.

An entry point (the Typer app, the build script, an embedding program, a test) makes one `Container` and
asks it for what it needs. Nothing is built at import and nothing is global: two containers with different
locations are independent, and a container made without locations reads them (`paths.load()`) the first time
a use case is asked for.
"""
from functools import cached_property

import add_fact
import building
import clock as clocks
import entities
import facts_batch
import lifecycle
import local_datafiles
import local_inputs
import migrate_memory
import music_export_store
import paths
import queries
import review
import rules_edit
import scripts
import subjects


class Container:
    def __init__(self, locations=None, clock=None):
        self._locations = locations
        self._clock = clock

    @cached_property
    def locations(self):
        return self._locations if self._locations is not None else paths.load()

    @cached_property
    def clock(self):
        """The clock every use case is given: the one passed in, else KNOWLEDGE_FROZEN_NOW or the system clock."""
        return self._clock if self._clock is not None else clocks.from_environment()

    @cached_property
    def inputs(self):
        """Read-only access to files the command line is pointed at (a facts JSON file for add-facts)."""
        return local_inputs

    @cached_property
    def datafiles(self):
        """The data files a maintenance tool reads and rewrites: locked, atomic, byte-exact (`datafiles_port`)."""
        return local_datafiles

    @property
    def music_export(self):
        """A fresh `MusicExportReads` over the built database; the caller closes it."""
        return music_export_store.open_reads(self.locations.db_path)

    @cached_property
    def review(self):
        return review.compose(self.locations, self.clock)

    @cached_property
    def lifecycle(self):
        return lifecycle.compose(self.locations, self.clock, self.review)

    @cached_property
    def add_fact(self):
        return add_fact.compose(self.locations, self.clock)

    @cached_property
    def facts_batch(self):
        return facts_batch.compose(self.locations, self.clock, self.add_fact)

    @cached_property
    def migrate_memory(self):
        return migrate_memory.compose(self.locations, self.clock, self.add_fact)

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
        return queries.compose(self.locations, self.clock)

    @cached_property
    def scripts(self):
        return scripts.compose(self.locations, self.clock)

    @cached_property
    def build_steps(self):
        """The step registry of the rebuild: every lane's registrations (`building.register_steps`) applied once."""
        return building.compose_registry()

    @cached_property
    def build_sessions(self):
        """The session factory of the rebuild: the one place a build database is opened, with the step context."""
        return building.compose_sessions(self.locations, inputs=self.inputs)

    def build_use_case(self, outputs, files=None):
        """The rebuild use case (`build_service`): `rebuild(emit)`, `check(emit)`, `build_database(path, sources, emit)`.
        `outputs` is the `build_port.BuildOutputs` the entry point supplies (the normal-db builder is pipeline code)."""
        if files is None:
            return building.compose(self.locations, self.clock, outputs, registry=self.build_steps,
                                    sessions=self.build_sessions)
        # `files` given (a test's, or an entry point's own): the registry and the session factory run over it too,
        # so every step is loaded and every connection opened through the one BuildFiles the caller supplied.
        return building.compose(self.locations, self.clock, outputs, files=files,
                                registry=building.compose_registry(files),
                                sessions=building.compose_sessions(self.locations, files, inputs=self.inputs))
