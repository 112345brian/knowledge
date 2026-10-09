"""Link facts to the measurements they cite (the `measurements` client source).

A fact entry in pilot_facts.json / facts_batch*.json may carry `measurement_metric_link`: the key of a metric
whose measurements back the fact. This step runs after the facts step and the measurements step and inserts a
`fact_measurements` row per measurement of that metric, for every fact that was loaded.
"""
from ingest import facts


def run(con):
    cur = con.cursor()
    linked = 0
    for item in facts.load_items():
        metric = item.get("measurement_metric_link")
        if not metric:
            continue
        row = cur.execute("SELECT id FROM facts WHERE source_key = ?", (item["_source_key"],)).fetchone()
        if row is None:
            continue   # the facts step skipped this entry (an invalid one), so there is nothing to link
        cur.execute(
            """INSERT INTO fact_measurements (fact_id, measurement_id)
               SELECT ?, m.id FROM measurements m JOIN metrics mt ON mt.id = m.metric_id WHERE mt.key = ?""",
            (row[0], metric))
        linked += cur.rowcount
    con.commit()
    print(f"[link_fact_measurements] {linked} fact <-> measurement links")
