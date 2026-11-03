"""The composition root of the whole application: one object that wires every use case for one `Locations` and one `Clock`.

An entry point (the Typer app, the build script, an embedding program, a test) makes one `Container` and
asks it for what it needs. Nothing is built at import and nothing is global: two containers with different
locations are independent, and a container made without locations reads them (`paths.load()`) the first time
a use case is asked for.
"""

__arbor__ = {"ring": "composition"}

from functools import cached_property

import add_fact
import building
import clock as clocks
import entities
import db_export
import db_snapshot
import docs
import facts_batch
import lifecycle
import local_datafiles
import local_inputs
import migrate_memory
import paths
import mcp_moded_store
import modes
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

    def db_export(self, db_path=None, directory=None):
        """Export and verify use cases for a database (default: the configured one), an export directory (default:
        `export/` in the private data directory) and the configured notes vault's `sources/` (for the notes part)."""
        vault = self.locations.notes_vault
        return db_export.compose_export(db_path or self.locations.db_path, directory or self.locations.in_data_dir("export"),
                                        self.locations.in_notes_vault("sources") if vault else None)

    def db_import(self, db_path, directory=None, notes_into=None):
        """The import use case: build a new database at `db_path` from an export directory (default as for
        `db_export`); notes, when asked for, are written into `notes_into`."""
        return db_export.compose_import(db_path, directory or self.locations.in_data_dir("export"), notes_into)

    def db_snapshots(self, db_path=None, directory=None):
        """Snapshot use cases for a database (default: the configured one) and a snapshot directory (default: `snapshots/`
        beside it)."""
        return db_snapshot.compose_snapshots(db_path or self.locations.db_path, directory or self.locations.snapshot_dir, self.clock)

    def db_restore_tables(self, db_path=None, snapshot_source=None, export_dir=None, snapshot_dir=None):
        """Table-restore use cases for a database (default: the configured one), reading rows from a snapshot file or an
        export folder (default folder: `export/` in the private data directory)."""
        return db_snapshot.compose_restore_tables(
            db_path or self.locations.db_path, snapshot_dir or self.locations.snapshot_dir, self.clock, snapshot_source,
            export_dir or self.locations.in_data_dir("export"))

    def docs_at(self, root=None):
        """Generated documentation use case, rooted at this checkout or an explicit repository copy."""
        return docs.compose(root)

    @cached_property
    def mcp_session(self):
        """The mode the MCP server runs under (`mcp_mode` in the config file): fixed for the life of the process."""
        return modes.Session(mode=self.locations.mcp_mode)

    @cached_property
    def mcp_lookup(self):
        return mcp_moded_store.ModedFactLookup(self.locations.db_path, self.mcp_session)

    @cached_property
    def mcp_capture(self):
        return mcp_moded_store.ModedCapture(self.add_fact, self.mcp_session)

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
