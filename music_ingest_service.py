"""The music client steps as application services: the concerts CSV, the RYM ratings export, the Last.fm scrobble
dump and the curated artist memberships. Each returns a `StepResult` and prints nothing; the step modules
(`client/concerts.py` ...) are thin driving adapters that print it. `music_ingest` is the composition root that
binds these functions to the SQLite adapter (`music_store`).

Artist identity: an artist seen live, rated on RYM and scrobbled is one `artists` row. Every importer resolves a
source name through an `ArtistDirectory`: the name is canonicalized by `artist_rules` (HTML unescape, then the
alias table) and looked up case-insensitively, because Last.fm, RYM and hand-typed concert names disagree on
casing far more often than SQL's ASCII-only NOCASE could fold. A directory is built per run and lives for exactly
one service call (it is a plain argument, never module state), so two runs can never share a stale cache.

Preserved from the scripts these replace: ids are assigned in the order names are first met, each importer
deletes the rows it previously loaded from its own import source (and only those) before loading, and a festival
is recognized by the names in artist_rules.json's `festivals`.
"""
import json
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from client import music_ingest_rules
from client.artist_rules import ArtistRules, artist_key, canonical_artist_name, parse_artist_rules
from inputs_port import Inputs
from locations import Locations
from music_repository_port import MusicRepository
from step_result import StepResult

ARTIST_RULES_FILENAME = "artist_rules.json"


@dataclass(frozen=True)
class MusicIngestDeps:
    """Everything these services reach the world through; the composition root supplies each one."""
    music: MusicRepository
    inputs: Inputs
    locations: Locations


def load_artist_rules(inputs: Inputs, data_dir: str) -> ArtistRules:
    """The optional aliases, group memberships and festival names from artist_rules.json in `data_dir`; no rules
    when the file is absent. RuntimeError naming the file when it cannot be read or is not version 1."""
    path = os.path.join(data_dir, ARTIST_RULES_FILENAME)
    if not inputs.exists(path):
        return ArtistRules()
    try:
        data = inputs.read_json(path)
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"{path}: cannot read artist rules: {e}") from e
    return parse_artist_rules(data, path)


class ArtistDirectory:
    """Artist ids for the names of one import run, keyed case-insensitively, created in first-seen order.

    `load` reads the artists already in the database (the first id wins when two differ only by case); `resolve`
    returns the id for a source name, adding the artist (under its canonical spelling) when it is new."""

    def __init__(self, music: MusicRepository, rules: ArtistRules, ids: Dict[str, int]):
        self._music = music
        self._rules = rules
        self._ids = ids

    @classmethod
    def load(cls, music: MusicRepository, rules: ArtistRules) -> "ArtistDirectory":
        ids: Dict[str, int] = {}
        for id_, name in music.artists():
            ids.setdefault(artist_key(name), id_)
        return cls(music, rules, ids)

    def resolve(self, name: str) -> int:
        name = canonical_artist_name(name, self._rules.aliases)
        key = artist_key(name)
        if key not in self._ids:
            self._ids[key] = self._music.add_artist(name)
        return self._ids[key]

    def __len__(self) -> int:
        return len(self._ids)


def import_concerts(deps: MusicIngestDeps) -> StepResult:
    """Replace this CSV's concert attendances with the file's rows (new artists, venues and festivals are added,
    never removed). A row without a name or start date is skipped."""
    music, inputs, locations = deps.music, deps.inputs, deps.locations
    source_id = music.import_source(locations.concerts_csv)
    music.forget_attendances(source_id)
    rules = load_artist_rules(inputs, locations.private_data_dir)
    artists = ArtistDirectory.load(music, rules)

    inserted = 0
    unresolved_festival_names = 0
    billing_counts: Dict[str, int] = {}
    for row in music_ingest_rules.csv_rows(inputs.read_text(locations.concerts_csv, "utf-8-sig")):
        concert = music_ingest_rules.concert_from_csv(row, rules.festivals)
        if concert is None:
            continue
        if concert["unresolved_festival"]:
            unresolved_festival_names += 1
        billing = concert["billing"]
        billing_counts[billing] = billing_counts.get(billing, 0) + 1

        artist_id = artists.resolve(concert["name"])
        venue_id = music.venue(concert["venue"], concert["city_state"]) if concert["venue"] else None
        festival_id = music.festival(concert["festival_name"]) if concert["festival_name"] else None
        supporting_id = artists.resolve(concert["supporting_for"]) if concert["supporting_for"] else None
        music.add_attendance(artist_id, concert["start_date"], concert["end_date"], venue_id, festival_id, billing,
                             supporting_id, concert["notes"], source_id)
        inserted += 1

    music.commit()
    counts = {"concert_attendances": inserted, "artists": music.count("artists"), "venues": music.count("venues"),
              "festivals": music.count("festivals"), "unresolved_festivals": unresolved_festival_names}
    lines = [f"[concerts] inserted {inserted} concert_attendances",
             f"  artists: {counts['artists']}, venues: {counts['venues']}, festivals: {counts['festivals']}",
             f"  festival rows with no resolvable festival name: {unresolved_festival_names} "
             "(Location held a bare city or similar -- not guessed)"]
    lines += [f"  billing={b}: {n}" for b, n in billing_counts.items()]
    return StepResult("concerts", counts, tuple(lines))


def import_ratings(deps: MusicIngestDeps) -> StepResult:
    """Replace this export's albums with the file's rows, on the artists shared with the concerts."""
    music, inputs, locations = deps.music, deps.inputs, deps.locations
    source_id = music.import_source(locations.rym_export_csv)
    music.forget_albums(source_id)
    rules = load_artist_rules(inputs, locations.private_data_dir)
    artists = ArtistDirectory.load(music, rules)

    inserted = 0
    for row in music_ingest_rules.csv_rows(inputs.read_text(locations.rym_export_csv, "utf-8-sig")):
        album = music_ingest_rules.album_from_row(row)
        if album is None:
            continue
        name, title, release_year, rating, rym_id = album
        music.add_album(artists.resolve(name), title, release_year, rating, rym_id, source_id)
        inserted += 1

    music.commit()
    counts = {"albums": inserted, "artists": music.count("artists")}
    return StepResult("music_ratings", counts, (f"[music_ratings] inserted {inserted} albums",
                                                f"  artists: {counts['artists']}"))


def import_scrobbles(deps: MusicIngestDeps) -> StepResult:
    """Replace this dump's scrobbles with the file's plays. The dump is optional: absent, nothing is touched.

    At ~150k rows this is the biggest ingest, so artists and tracks are looked up in memory (the directory and a
    track cache) and the plays are written in one batch. Group or collaborative credits stay one artist each;
    membership is curated separately (`seed_artist_members`)."""
    music, inputs, locations = deps.music, deps.inputs, deps.locations
    path = locations.scrobbles_json
    if not inputs.exists(path):
        return StepResult("scrobbles", {}, (f"[scrobbles] {path} not found, skipping",), skipped=True)

    source_id = music.import_source(path)
    music.forget_scrobbles(source_id)
    scrobbles = list(music_ingest_rules.scrobbles_from_pages(inputs.read_json(path)))

    rules = load_artist_rules(inputs, locations.private_data_dir)
    artists = ArtistDirectory.load(music, rules)
    before_artists = len(artists)
    artist_ids: Dict[str, int] = {}
    for artist, *_ in scrobbles:
        if artist not in artist_ids:
            artist_ids[artist] = artists.resolve(artist)
    new_artists = len(artists) - before_artists

    track_ids: Dict[Tuple[int, str, str], int] = {
        (artist_id, title, album): id_ for id_, artist_id, title, album in music.tracks()}
    before_tracks = len(track_ids)
    rows: List[Tuple[int, str, int]] = []
    for artist, track, album, played_at in scrobbles:
        key = (artist_ids[artist], track, album)
        if key not in track_ids:
            track_ids[key] = music.add_track(*key)
        rows.append((track_ids[key], played_at, source_id))
    new_tracks = len(track_ids) - before_tracks

    music.add_scrobbles(rows)
    music.commit()
    counts = {"parsed": len(scrobbles), "new_artists": new_artists, "new_tracks": new_tracks,
              "scrobbles": music.count("scrobbles"), "tracks": music.count("tracks"), "artists": music.count("artists")}
    return StepResult("scrobbles", counts, (
        f"[scrobbles] parsed {len(scrobbles)} scrobbles, {new_artists} new artists, {new_tracks} new tracks",
        f"  scrobbles: {counts['scrobbles']}, tracks: {counts['tracks']}, artists: {counts['artists']}"))


def seed_artist_members(deps: MusicIngestDeps) -> StepResult:
    """Record the curated group memberships of artist_rules.json as artist_members rows. Runs after the imports so
    a member already exists from their own credits; a group or member not yet known is created."""
    music = deps.music
    rules = load_artist_rules(deps.inputs, deps.locations.private_data_dir)
    artists = ArtistDirectory.load(music, rules)
    inserted = 0
    for group_name, member_names in rules.members.items():
        group_id = artists.resolve(group_name)
        for member_name in member_names:
            if music.add_member(group_id, artists.resolve(member_name)):
                inserted += 1
    music.commit()
    return StepResult("seed_artist_members", {"artist_members": inserted},
                      (f"[seed_artist_members] inserted {inserted} artist_members rows",))
