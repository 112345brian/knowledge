"""Composition root of the database rebuild: binds `build_service` to its adapters and owns the session factory and
the step registry.

  * `Sessions` opens the build database and hands the steps what they get (the connection and a `StepContext`).
  * `Registry` is the composed step registry. Every step is still run the way it always was (its script is loaded
    and `run(con, ctx)` called) until a subsystem registers its own handler for it with `register`; the same change
    that registers a handler deletes nothing else, because a registered step simply never takes the file path.
    A subsystem switches by calling `registry.register("ingest/facts.py", facts.run_step)`, in the registration
    function its lane adds to this module's `register_steps`.
  * `compose` wires them with the provenance reader and the outputs (the table report and knowledge-normal.db,
    which `build.py` supplies because the normal-db builder is part of the pipeline layer).
"""
import os

import build_info_store
import build_service
import client_bootstrap
import local_buildfiles
import local_inputs
import local_stores
import private_git
from build_port import StepResult
from pipeline_context import StepContext
from wiring import bind_all

API = ("build_database", "build_to_temp", "rebuild", "check")


class Session:
    """One build database: its connection and the step context. Implements `build_port.BuildSession`."""

    def __init__(self, files, code_dir, con, context, clients):
        self._files, self._code_dir, self._clients = files, code_dir, clients
        self.con, self.context = con, context

    def apply_schema(self, sources):
        """schema.sql plus the fragment of each enabled client source, in order."""
        text = "\n".join(self._files.read_text(self._files.join(self._code_dir, name))
                         for name in self._clients.schema_files(sources))
        self.con.executescript(text)

    def commit(self):
        self.con.commit()

    def optimize(self):
        self.con.execute("VACUUM;")
        self.con.execute("ANALYZE;")

    def close(self):
        self.con.close()


class Sessions:
    """The session factory (`build_port.SessionFactory`): the one place a build database connection is opened."""

    def __init__(self, files, code_dir, context, clients):
        self._files, self._code_dir, self._context, self._clients = files, code_dir, context, clients

    def open(self, path):
        con = self._files.open_database(path)
        try:
            con.execute("PRAGMA foreign_keys = ON;")
        except BaseException:
            con.close()
            raise
        return Session(self._files, self._code_dir, con, self._context, self._clients)


class Registry:
    """The step registry (`build_port.StepRegistry`). `order` is the pipeline's order for the enabled client
    sources (or `only`, a fixed list of step files, for a synthetic build). `run` calls the handler registered for
    a step, or else runs the step the legacy way: load its script from `code_dir`, call `run(con, context)`."""

    def __init__(self, files, code_dir, only=None, clients=None):
        self._files, self._code_dir = files, code_dir
        self._clients = clients or client_bootstrap.registry(files, code_dir)
        self._only = None if only is None else tuple(only)
        self._known = set(self._clients.all_steps()) | set(self._only or ())
        self._handlers = {}

    def register(self, step, handler):
        """Run `handler(session)` for `step` instead of loading its script. `step` is its PIPELINE path."""
        if step not in self._known:
            raise ValueError(f"{step!r} is not a build step; known: {sorted(self._known)}")
        if step in self._handlers:
            raise ValueError(f"{step!r} already has a registered handler")
        self._handlers[step] = handler

    def registered(self):
        return tuple(sorted(self._handlers))

    def order(self, sources):
        return list(self._clients.steps_for(sources) if self._only is None else self._only)

    def run(self, step, session):
        handler = self._handlers.get(step)
        if handler is not None:
            handler(session)
            return StepResult(step, "registered")
        module = self._files.load_module(self._files.join(self._code_dir, step))
        module.run(session.con, session.context)
        return StepResult(step, "file")


def code_dir():
    """The checkout the pipeline runs from: where schema.sql and the step scripts are."""
    return os.path.dirname(os.path.abspath(__file__))


def register_steps(registry):
    """Where each subsystem lane registers the steps it has moved off the legacy file path. Empty until a lane
    switches; a lane adds its `registry.register(...)` lines here and removes the step's legacy path in the same change."""
    return registry


def compose_clients(files=None, code_dir_path=None):
    """The client sources the client directory registers (none when there is no client directory)."""
    return client_bootstrap.registry(files or local_buildfiles, code_dir_path or code_dir())


def compose_registry(files=None, code_dir_path=None, only=None, clients=None):
    """The step registry with every lane's registrations applied."""
    files, here = files or local_buildfiles, code_dir_path or code_dir()
    return register_steps(Registry(files, here, only, clients or compose_clients(files, here)))


def compose_sessions(locations, files=None, code_dir_path=None, inputs=None, stores=None, clients=None):
    """The session factory for these `locations`."""
    files, here = files or local_buildfiles, code_dir_path or code_dir()
    return Sessions(files, here, StepContext(locations, inputs or local_inputs, stores or local_stores),
                    clients or compose_clients(files, here))


def compose(locations, clock, outputs, files=None, code_dir_path=None, registry=None, provenance=None, sessions=None,
            clients=None):
    """The build use case for these `locations` and this `clock`. `outputs` is a `build_port.BuildOutputs`. The
    others default to the real adapters; a test passes fakes for any of them."""
    files = files or local_buildfiles
    here = code_dir_path or code_dir()
    clients = clients or compose_clients(files, here)
    deps = build_service.BuildDeps(
        files=files, clock=clock, locations=locations,
        sessions=sessions or compose_sessions(locations, files, here, clients=clients),
        steps=registry or compose_registry(files, here, clients=clients),
        provenance=provenance or build_info_store.BuildProvenance(locations, here, private_git, clients),
        outputs=outputs, clients=clients)
    return bind_all(build_service, deps, API)
