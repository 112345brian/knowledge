"""Composition root for the client music steps: binds `music_service` to the SQLite music adapter for the
build connection a step is given. Nothing is built at import; each step composes its own for its own run."""
from client import music_service, music_store
from client.music_port import MusicRepository
from wiring import bind_all



def ports():
    """The music source's own ports by dependency name, handed to wiring (the core's registry does not know them)."""
    return {"music": MusicRepository}

API = ("import_concerts", "import_ratings", "import_scrobbles", "seed_artist_members")


def compose(con, inputs, locations):
    """The music services wired to `con` (the build database), reading files through `inputs`. The connection is
    the unit of work: a step commits it once its writes are complete."""
    deps = music_service.MusicIngestDeps(music=music_store.SqliteMusicRepository(con), unit_of_work=con,
                                         inputs=inputs, locations=locations)
    return bind_all(music_service, deps, API, ports=ports())
