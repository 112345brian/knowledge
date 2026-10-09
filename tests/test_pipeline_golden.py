"""The whole ingest pipeline (steps 01-12) on a synthetic world must produce exactly the rows it produced
before the pipeline was refactored onto domain rules + adapters (tests/golden/pipeline_dump.json, generated
from the pre-refactor code). A change to what gets ingested has to be a deliberate edit of that file."""
import json
import os

from pipeline_world import REPO, run_pipeline

GOLDEN = os.path.join(REPO, "tests", "golden", "pipeline_dump.json")


def test_pipeline_output_matches_the_golden_dump(tmp_path):
    got = run_pipeline(tmp_path)
    want = json.load(open(GOLDEN))
    assert sorted(got) == sorted(want), sorted(set(got) ^ set(want))
    for table in want:
        assert got[table]["columns"] == want[table]["columns"], table
        assert got[table]["rows"] == want[table]["rows"], f"{table} differs"


def test_the_world_exercises_every_ingest_step(tmp_path):
    got = run_pipeline(tmp_path)
    for table in ("sources", "authors", "measurements", "metrics", "facts", "fact_sources", "fact_revisions",
                  "fact_measurements", "claims", "claim_facts", "concert_attendances", "venues", "festivals", "albums",
                  "scrobbles", "tracks", "artist_members", "training_sets", "food_log_entries", "meal_log_entries",
                  "muscle_volume_weekly", "vault_files", "subjects"):
        assert got.get(table, {}).get("rows"), f"{table} is empty: the golden test would not notice a break there"
