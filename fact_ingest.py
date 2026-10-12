"""The fact ingest (the facade: binds the use case in `fact_ingest_service` to the adapters a build step is given).

    api = fact_ingest.compose(ctx, con)        # ctx: the pipeline_context.StepContext of the step; con: the build database
    report = api.ingest_fact_files(con, warn=print)
    for line in fact_ingest_decisions.render(report): print(line)

The repositories (`build_repositories_port`) are the ones the step's `stores` hands out, the inputs and privacy rules are the ones the step already
has, so the step imports no infrastructure module. Which subject a name means and which vault file a path is are not
decided here: `BuildIdentity` asks `subject_hierarchy_service` and `source_ingest_service`, the build's one definition.
"""
from types import SimpleNamespace

import fact_ingest_service
import source_ingest_service
import subject_hierarchy_service
from fact_ingest_service import FactIngestDeps
from wiring import bind_all

API = ("ingest_fact_files", "ingest_general_facts")


class BuildIdentity:
    """`fact_ingest_port.Identity` over the build connection's subject and source repositories."""
    def __init__(self, subjects, sources, inputs):
        self._hierarchy = subject_hierarchy_service.HierarchyDeps(subjects=subjects)
        self._sources = sources
        self._inputs = inputs

    def subject_id(self, name, domain=None, resolve_aliases=False):
        return subject_hierarchy_service.subject_for_fact(self._hierarchy, name, domain, resolve_aliases=resolve_aliases)

    def vault_file_id(self, path):
        return source_ingest_service.vault_file_id(self._sources, self._inputs, path)


def compose(ctx, con):
    """The fact-ingest API for the step context `ctx`, over the build connection `con`."""
    stores = ctx.stores
    identity = BuildIdentity(stores.subject_repository(con), stores.source_repository(con), ctx.inputs)
    deps = FactIngestDeps(facts=stores.fact_repository(con), revision_writer=stores.revision_writer(con),
                          sources=stores.source_repository(con), unit_of_work=stores.unit_of_work(con),
                          identity=identity, inputs=ctx.inputs, privacy_floor=stores, locations=ctx.locations)
    return bind_all(fact_ingest_service, deps, API)


def load_fact_file_entries(ctx):
    """Every entry of the fact files, in file order, each with its source key: the readers need no database."""
    return fact_ingest_service.load_fact_file_entries(SimpleNamespace(inputs=ctx.inputs, locations=ctx.locations))
