"""What the measurements step and the fact-link step write, pinned before the port collapse (and kept after it):
every table they touch, ids included, against tests/golden/measurements_step_rows.json, plus the edge inputs a
decision moved out of an adapter could change: blank and NUL names, a second run, a missing source, no items.
The fixture vault is test_measurements_dates'; extra rows here exercise the event tables."""
import json
import os
import sqlite3

import pytest

import test_measurements_dates as dates
from test_measurements_dates import m03  # noqa: F401  (fixture)
from schema_helper import REPO

GOLDEN = os.path.join(REPO, "tests", "golden", "measurements_step_rows.json")
TABLES = ("subjects", "metrics", "exercises", "foods", "muscles", "authors", "source_authors", "measurements",
          "training_sets", "food_log_entries", "meal_log_entries", "muscle_volume_weekly")

EXTRA = {
    "workout_sets": [("2026-02-03", "Squat", 225, "lb", 5, 2, 0, None), ("2026-02-03", None, 100, None, 8, None, 1, "warm"),
                     ("2026-02-04", "Bench\x00Press", 135, "kg", 5, None, 0, "nul in the name")],
    "jefit_exercise_sets": [("2020-01-01", "Squat", 135, 5, "kg"), ("2020-01-02", "", 95, 8, None)],
    "mfp_nutrition_log": [("2026-02-04", "Lunch", *range(1, 15))],
    "nutrition_food_log": [("2026-02-04", "12:00", "Rice", "cup", 1, 200, 1, 45, 4, 0),
                           ("2026-02-04", "13:00", None, "g", 100, 50, 0, 10, 1, 0),
                           ("2026-02-04", "14:00", "", "g", 10, 5, 0, 1, 0, 0),
                           ("2026-02-04", "15:00", "Rice", "cup", 2, 400, 2, 90, 8, 0)],
    "muscle_volume_weekly": [("2026-02-02", 10, None), ("2026-02-09", None, 6)],
}


@pytest.fixture
def extra_rows(monkeypatch):
    """The vault is built when m03 is, so the extra rows go in first (fixtures run in argument order)."""
    rows = dict(dates.ROWS)
    rows.update(EXTRA)
    monkeypatch.setattr(dates, "ROWS", rows)


@pytest.fixture
def rich(extra_rows, m03, tmp_path):  # noqa: F811
    m03.vault_dir = str(tmp_path)
    m03.write_snapshot('{"synced_at": "2026-09-11"}')
    return m03


def _rows(con, sql, mask=False):
    cur = con.execute(sql)
    stamp = [d[0] for d in cur.description].index("date_added") if mask and "date_added" in [d[0] for d in cur.description] else None
    return [["<build timestamp>" if i == stamp else v for i, v in enumerate(r)] for r in cur]


def dump(con):
    out = {}
    for t in TABLES:
        order = "source_id, author_id" if t == "source_authors" else "id"
        out[t] = _rows(con, f"SELECT * FROM {t} ORDER BY {order}", mask=t != "measurements")
    out["sources"] = _rows(con, "SELECT id, citekey, name, source_type, publisher_id, url, published_date, retrieved_date, "
                                "description, origin_path, status FROM sources ORDER BY id")
    return out


def test_every_table_the_step_writes_is_pinned_ids_included(rich):
    got = json.loads(json.dumps(dump(rich.build(written="~/vault"))))
    if not os.path.exists(GOLDEN):
        with open(GOLDEN, "w", encoding="utf-8") as f:
            json.dump(got, f, indent=1, sort_keys=True)
            f.write("\n")
    with open(GOLDEN, encoding="utf-8") as f:
        want = json.load(f)
    assert got == want


def test_blank_and_missing_names_fall_back_and_share_one_row(rich):
    con = rich.build()
    assert [r[0] for r in con.execute("SELECT name FROM foods ORDER BY id")] == ["Rice", "Unknown food"]
    assert "Unknown exercise" in [r[0] for r in con.execute("SELECT name FROM exercises")]
    assert "Bench\x00Press" in [r[0] for r in con.execute("SELECT name FROM exercises")]
    assert con.execute("SELECT COUNT(*) FROM food_log_entries").fetchone()[0] == 4


def test_a_second_run_over_the_same_database_reuses_dimension_rows_and_sources(rich):
    con = rich.build()
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    sources = con.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    rich.module.run(con, _ctx(rich))
    again = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    assert con.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == sources
    for t in ("subjects", "metrics", "exercises", "foods", "muscles", "authors", "source_authors"):
        assert again[t] == counts[t], t
    assert again["measurements"] == 2 * counts["measurements"]


def _ctx(m03):
    from appctx import locs
    return locs(notes_vault=m03.vault_dir, notes_vault_written=None)


def test_a_missing_manual_source_writes_nothing_to_the_event_tables(rich):
    from appctx import locs
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(__import__("schema_helper").full_schema())
    with open(os.path.join(rich.env.data_dir, "measurements_config.json"), "w") as f:
        json.dump({"version": 1, "manual_sources": {"dexa-2026": "nope"}, "scans": []}, f)
    rich.module.DATA_DIR = rich.env.data_dir
    with pytest.raises(RuntimeError, match="nope.*seed_sources"):
        rich.module.run(con, locs(notes_vault=rich.vault_dir, notes_vault_written=None))
    assert con.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0


# ---------------------------------------------------------------- the link step

def _linked_db(rich):
    con = rich.build()
    subject = con.execute("SELECT id FROM subjects LIMIT 1").fetchone()[0]
    con.execute("INSERT INTO facts (statement, subject_id, source_key, trust_level, freshness) "
                "VALUES ('f', ?, 'p-1', 'high', 'unreviewed')", (subject,))
    con.commit()
    return con


def test_the_link_step_links_loaded_facts_and_skips_unloaded_or_blank_ones(rich, monkeypatch):
    from client import link_fact_measurements as step
    con = _linked_db(rich)
    items = [{"_source_key": "p-1", "measurement_metric_link": "weight_lb"},
             {"_source_key": "p-1"},
             {"_source_key": "p-1", "measurement_metric_link": ""},
             {"_source_key": "missing", "measurement_metric_link": "weight_lb"},
             {"_source_key": "p-1", "measurement_metric_link": "no_such_metric"}]
    monkeypatch.setattr(step.facts, "load_items", lambda ctx: items)
    step.run(con, object())
    n = con.execute("SELECT COUNT(*) FROM measurements m JOIN metrics mt ON mt.id = m.metric_id "
                    "WHERE mt.key = 'weight_lb'").fetchone()[0]
    assert n > 0
    assert con.execute("SELECT COUNT(*) FROM fact_measurements").fetchone()[0] == n


def test_the_link_step_with_no_items_commits_and_links_nothing(rich, monkeypatch):
    from client import link_fact_measurements as step
    con = _linked_db(rich)
    monkeypatch.setattr(step.facts, "load_items", lambda ctx: [])
    step.run(con, object())
    assert con.execute("SELECT COUNT(*) FROM fact_measurements").fetchone()[0] == 0
