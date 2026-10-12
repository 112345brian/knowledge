"""The client's registration with the build: which sources exist here, where their steps run, what they need.

`client_bootstrap` loads this module once the `client/` directory is recognised and calls `register(registry)`.
Adding a source of your own is a module (or several) in this directory plus one `registry.add(ClientSource(...))`
below; no file outside `client/` changes, so updating the repository does not touch it. The base never imports
this module by name from its own code; it is loaded by path.

Domain ring declaration only: it builds values from `client_registry`, does no I/O.
"""
from client_registry import ClientRegistry, ClientSource, Input, Step, Tool

AFTER_FACTS = "ingest/facts.py"
AFTER_SUBJECTS = "ingest/seed_subject_hierarchy.py"

_MUSIC_TABLES = ("artists", "artist_members", "venues", "festivals", "concert_attendances", "albums", "tracks",
                 "import_sources", "scrobbles")
_MEASUREMENT_TABLES = ("metrics", "measurements", "fact_measurements", "exercises", "training_sets", "foods",
                       "food_log_entries", "meal_log_entries", "muscles", "muscle_volume_weekly")
_MUSIC_REPORT_AFTER = dict(zip(
    _MUSIC_TABLES,
    ("muscle_volume_weekly", "artists", "artist_members", "venues", "festivals", "concert_attendances",
     "albums", "tracks", "import_sources")))
_MEASUREMENT_REPORT_AFTER = dict(zip(
    _MEASUREMENT_TABLES,
    ("publishers", "metrics", "fact_revisions", "fact_measurements", "exercises", "training_sets", "foods",
     "food_log_entries", "meal_log_entries", "muscles")))

# Shared by the three music sources: declared in each, applied once.
_MUSIC_SCHEMA = ("client/music.sql",)
_MUSIC_MEMBERS = Step("client/seed_artist_members.py", after=AFTER_SUBJECTS, order=40)
_MUSIC_DATA = ("artist_rules.json",)


def register(registry: ClientRegistry) -> None:
    registry.add(ClientSource(
        "concerts", steps=(Step("client/concerts.py", after=AFTER_SUBJECTS, order=10), _MUSIC_MEMBERS),
        schema=_MUSIC_SCHEMA, inputs=(Input("CONCERTS_CSV", "concerts-csv", required=True),),
        report_tables=_MUSIC_TABLES, report_after=_MUSIC_REPORT_AFTER, data_files=_MUSIC_DATA,
        tools=(Tool("clean-concerts", "client.clean_concerts_csv", "Clean concerts.csv in place (dedupes rows)."),),
        leak_rows=(("INSERT INTO artists (id, name) VALUES (1, 'Secret Band')", ()),)))
    registry.add(ClientSource(
        "ratings", steps=(Step("client/music_ratings.py", after=AFTER_SUBJECTS, order=20), _MUSIC_MEMBERS),
        schema=_MUSIC_SCHEMA, inputs=(Input("RYM_EXPORT_CSV", "rym-export-csv", required=True),),
        report_tables=_MUSIC_TABLES, report_after=_MUSIC_REPORT_AFTER, data_files=_MUSIC_DATA))
    registry.add(ClientSource(
        "scrobbles", steps=(Step("client/scrobbles.py", after=AFTER_SUBJECTS, order=30), _MUSIC_MEMBERS),
        schema=_MUSIC_SCHEMA, inputs=(Input("SCROBBLES_JSON", "scrobbles-json", required=True),),
        report_tables=_MUSIC_TABLES, report_after=_MUSIC_REPORT_AFTER, data_files=_MUSIC_DATA))
    registry.add(ClientSource(
        "measurements",
        steps=(Step("client/measurements.py", after="ingest/literature_sources.py"),
               Step("client/link_fact_measurements.py", after=AFTER_FACTS, order=10)),
        schema=("client/measurements.sql",),
        inputs=(Input("NOTES_VAULT", "vault-db", file="bodybuilding.db"),),
        report_tables=_MEASUREMENT_TABLES, report_after=_MEASUREMENT_REPORT_AFTER,
        data_files=("measurements_snapshot.json", "measurements_config.json"),
        personal_fact_keys=("measurement_metric_link",)))
    registry.add(ClientSource("claims", steps=(Step("client/seed_claims.py", after=AFTER_FACTS, order=20),),
                              data_files=("claims.json",)))
    # A local_paths.py written before CLIENT_SOURCES existed but defining the music inputs built everything.
    registry.enable_when_defined("CONCERTS_CSV", ("concerts", "ratings", "scrobbles", "measurements", "claims"))
