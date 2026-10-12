"""Composition root for the client music steps: binds `music_ingest_service` to the SQLite music adapter for the
build connection a step is given. Nothing is built at import; each step composes its own for its own run."""
import music_ingest_service
import music_store
from wiring import bind_all

API = ("import_concerts", "import_ratings", "import_scrobbles", "seed_artist_members")


def compose(con, inputs, locations):
    """The music services wired to `con` (the build database), reading files through `inputs`. The connection is
    the unit of work: a step commits it once its writes are complete."""
    deps = music_ingest_service.MusicIngestDeps(music=music_store.SqliteMusicRepository(con), unit_of_work=con,
                                                inputs=inputs, locations=locations)
    return bind_all(music_ingest_service, deps, API)
