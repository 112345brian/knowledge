"""In-memory stand-ins for the music repository and the Inputs port, for application tests that need no sqlite."""
import json


class FakeInputs:
    """`Inputs` over a dict of path -> text (JSON files are stored as text and parsed on read)."""

    def __init__(self, files=None):
        self.files = dict(files or {})
        self.reads = []

    def exists(self, path):
        return path in self.files

    def read_text(self, path, encoding="utf-8"):
        self.reads.append(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def read_json(self, path):
        return json.loads(self.read_text(path))

    def list_files(self, directory, pattern):
        return []

    def open_database(self, path):
        raise AssertionError("the music steps open no database of their own")

    def fingerprint(self, path):
        raise AssertionError("not used")

    def acquisition(self, row, path):
        raise AssertionError("not used")


class FakeMusicRepository:
    """Implements `MusicRepository` over lists; `commits` counts commits and `forgotten` the replacements."""

    def __init__(self, artists=(), tracks=()):
        self._artists = list(artists)            # [(id, name)]
        self._next = {"artist": max([i for i, _ in self._artists], default=0) + 1}
        self.sources = {}                        # path -> id
        self.venues = {}                         # (name, city_state) -> id
        self.festivals = {}                      # name -> id
        self.attendances = []                    # dicts
        self.albums = []
        self.track_rows = list(tracks)           # [(id, artist_id, title, album)]
        self.scrobbles_by_source = {}            # source id -> {(track_id, played_at)}, deduped like INSERT OR IGNORE
        self.members = set()
        self.commits = 0
        self.forgotten = []

    # --- port
    def import_source(self, path):
        return self.sources.setdefault(path, len(self.sources) + 1)

    def forget_attendances(self, import_source_id):
        self.forgotten.append(("attendances", import_source_id))
        self.attendances = [a for a in self.attendances if a["import_source_id"] != import_source_id]

    def forget_albums(self, import_source_id):
        self.forgotten.append(("albums", import_source_id))
        self.albums = [a for a in self.albums if a["import_source_id"] != import_source_id]

    def forget_scrobbles(self, import_source_id):
        self.forgotten.append(("scrobbles", import_source_id))
        self.scrobbles_by_source.pop(import_source_id, None)

    def artists(self):
        return list(self._artists)

    def add_artist(self, name):
        if any(n == name for _, n in self._artists):
            raise AssertionError(f"artists.name is UNIQUE: {name!r} added twice")
        id_ = self._next["artist"]
        self._next["artist"] += 1
        self._artists.append((id_, name))
        return id_

    def venue(self, name, city_state):
        return self.venues.setdefault((name, city_state), len(self.venues) + 1)

    def festival(self, name):
        return self.festivals.setdefault(name, len(self.festivals) + 1)

    def add_attendance(self, artist_id, start_date, end_date, venue_id, festival_id, billing, supporting_artist_id,
                       notes, import_source_id):
        self.attendances.append(dict(artist_id=artist_id, start_date=start_date, end_date=end_date, venue_id=venue_id,
                                     festival_id=festival_id, billing=billing, supporting=supporting_artist_id,
                                     notes=notes, import_source_id=import_source_id))

    def add_album(self, artist_id, title, release_year, rating, rym_id, import_source_id):
        self.albums.append(dict(artist_id=artist_id, title=title, release_year=release_year, rating=rating,
                                rym_id=rym_id, import_source_id=import_source_id))

    def tracks(self):
        return list(self.track_rows)

    def add_track(self, artist_id, title, album):
        id_ = max([t[0] for t in self.track_rows], default=0) + 1
        self.track_rows.append((id_, artist_id, title, album))
        return id_

    def add_scrobbles(self, rows):
        for track_id, played_at, source_id in rows:
            self.scrobbles_by_source.setdefault(source_id, set()).add((track_id, played_at))

    def add_member(self, artist_id, member_id):
        if (artist_id, member_id) in self.members:
            return False
        self.members.add((artist_id, member_id))
        return True

    def count(self, what):
        return {"artists": len(self._artists), "venues": len(self.venues), "festivals": len(self.festivals),
                "tracks": len(self.track_rows),
                "scrobbles": sum(len(v) for v in self.scrobbles_by_source.values())}[what]

    def commit(self):
        self.commits += 1

    # --- helpers for assertions
    def artist_names(self):
        return [n for _, n in self._artists]
