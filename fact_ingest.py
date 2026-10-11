"""The fact ingest (the facade: binds the use case in `fact_ingest_service` to the adapters a build step is given).

    api = fact_ingest.compose(ctx)             # ctx: the pipeline_context.StepContext of the step
    report = api.ingest_fact_files(con)
    for line in fact_ingest_decisions.render(report): print(line)

The fact repository is the one the step's `stores` hands out (`fact_repository()`), the inputs and privacy rules
are the ones the step already has, so the step imports no infrastructure module.
"""
import fact_ingest_service
from fact_ingest_service import FactIngestDeps
from wiring import bind_all

API = ("ingest_fact_files", "ingest_general_facts", "load_fact_file_entries", "load_general_entries", "ensure_vault_file")


def compose(ctx):
    """The fact-ingest API for the step context `ctx` (locations, inputs, stores)."""
    deps = FactIngestDeps(facts=ctx.stores.fact_repository(), inputs=ctx.inputs, privacy_floor=ctx.stores,
                          locations=ctx.locations)
    return bind_all(fact_ingest_service, deps, API)
