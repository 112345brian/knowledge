"""Link facts to the measurements they cite (the `measurements` client source).

A fact entry in pilot_facts.json / facts_batch*.json may carry `measurement_metric_link`: the key of a metric
whose measurements back the fact. This step runs after the facts step and the measurements step and inserts a
`fact_measurements` row per measurement of that metric, for every fact that was loaded.
"""
from client import measurements_service, measurements_store
from ingest import facts


def run(con, ctx):
    deps = measurements_service.FactLinksDeps(facts=measurements_store.SqliteFactMeasurementLinks(con), unit_of_work=con)
    linked = measurements_service.link_fact_measurements(deps, facts.load_items(ctx))
    print(f"[link_fact_measurements] {linked} fact <-> measurement links")
