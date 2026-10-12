"""The music client steps as application services: the concerts CSV, the RYM ratings export, the Last.fm scrobble
dump and the curated artist memberships. Each returns a `StepResult` and prints nothing; the step modules
(`client/concerts.py` ...) are thin driving adapters that print it. `client.music_ingest` is the composition root that
binds these functions to the SQLite adapter (`music_store`).

The service talks to the client's `MusicRepository` (`client.music_port`) and decides everything the adapter
does not: it maps a CSV row or a scrobble to the records the repository stores, decides which rows a rerun
replaces (its own import source's, and only those) and commits through the unit of work once a step's writes are
complete (never when it fails before writing).

Artist identity: an artist seen live, rated on RYM and scrobbled is one `artists` row. Every importer resolves a
source name through `client.artist_identity_service.ArtistIdentity`: the name is canonicalized by `client.artist_rules` (HTML
unescape, then the alias table) and looked up case-insensitively, because Last.fm, RYM and hand-typed concert names
disagree on casing far more often than SQL's ASCII-only NOCASE could fold. An identity is built per run and lives
for exactly one service call (a plain local, never module state), so two runs can never share a stale cache.

Ordering contract (ids are allocated in call order, so the order of these calls is the database's content): the
import source, then the deletion of its rows, then the rules file, then each row in file order; within a concert
the artist, the venue, the festival, then the supporting artist.

Preserved from the scripts these replace: ids are assigned in the order names are first met, each importer
deletes the rows it previously loaded from its own import source (and only those) before loading, and a festival
is recognized by the names in artist_rules.json's `festivals`.
"""
import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from build_repositories_port import UnitOfWork
from build_steps_port import StepResult
from client import music_ingest_rules
from client import music_records as r
from client.artist_identity_service import ArtistIdentity
from client.artist_rules import parse_artist_rules
from client.music_port import MusicRepository
from inputs_port import Inputs
from locations import Locations

ARTIST_RULES_FILENAME = "artist_rules.json"


@dataclass(frozen=True)
class MusicIngestDeps:
    """Everything these services reach the world through; the composition root supplies each one."""
    music: MusicRepository
    unit_of_work: UnitOfWork
    inputs: Inputs
    locations: Locations


def load_artist_rules(inputs: Inputs, locations: Locations) -> r.ArtistRules:
    """The optional aliases, group memberships and festival names from artist_rules.json in the private data dir;
    no rules when the file is absent. RuntimeError naming the file when it cannot be read or is not version 1."""
    path = locations.in_data_dir(ARTIST_RULES_FILENAME)
    if not inputs.exists(path):
        return r.ArtistRules()
    try:
        data = inputs.read_json(path)
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"{path}: cannot read artist rules: {e}") from e
    return parse_artist_rules(data, path)


def import_concerts(deps: MusicIngestDeps) -> StepResult:
    """Replace this CSV's concert attendances with the file's rows (new artists, venues and festivals are added,
    never removed). A row without a name or start date is skipped."""
    music, inputs, locations = deps.music, deps.inputs, deps.locations
    csv_path = locations.need("concerts_csv", "CONCERTS_CSV", "the concerts import")
    source_id = music.import_source(locations.concerts_csv_stored)   # stored as written (`~/...`), read from the expanded path
    music.delete_attendances(source_id)
    rules = load_artist_rules(inputs, locations)
    artists = ArtistIdentity(music, rules)

    inserted = 0
    unresolved_festival_names = 0
    billing_counts: Dict[str, int] = {}
    for row in music_ingest_rules.csv_rows(inputs.read_text(csv_path, "utf-8-sig")):
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
        music.add_attendance(r.NewAttendance(
            artist_id=artist_id, start_date=concert["start_date"], end_date=concert["end_date"], venue_id=venue_id,
            festival_id=festival_id, billing=billing, supporting_for_artist_id=supporting_id, notes=concert["notes"],
            import_source_id=source_id))
        inserted += 1

    deps.unit_of_work.commit()
    totals = music.totals()
    counts = {"concert_attendances": inserted, "artists": totals.artists, "venues": totals.venues,
              "festivals": totals.festivals, "unresolved_festivals": unresolved_festival_names}
    lines = [f"[concerts] inserted {inserted} concert_attendances",
             f"  artists: {counts['artists']}, venues: {counts['venues']}, festivals: {counts['festivals']}",
             f"  festival rows with no resolvable festival name: {unresolved_festival_names} "
             "(Location held a bare city or similar -- not guessed)"]
    lines += [f"  billing={b}: {n}" for b, n in billing_counts.items()]
    return StepResult("concerts", lines=tuple(lines), counts=counts)


def import_ratings(deps: MusicIngestDeps) -> StepResult:
    """Replace this export's albums with the file's rows, on the artists shared with the concerts."""
    music, inputs, locations = deps.music, deps.inputs, deps.locations
    csv_path = locations.need("rym_export_csv", "RYM_EXPORT_CSV", "the ratings import")
    source_id = music.import_source(locations.rym_export_csv_stored)
    music.delete_albums(source_id)
    rules = load_artist_rules(inputs, locations)
    artists = ArtistIdentity(music, rules)

    inserted = 0
    for row in music_ingest_rules.csv_rows(inputs.read_text(csv_path, "utf-8-sig")):
        album = music_ingest_rules.album_from_row(row)
        if album is None:
            continue
        name, title, release_year, rating, rym_id = album
        music.add_album(r.NewAlbum(artist_id=artists.resolve(name), title=title, release_year=release_year,
                                   rating=rating, rym_id=rym_id, import_source_id=source_id))
        inserted += 1

    deps.unit_of_work.commit()
    counts = {"albums": inserted, "artists": music.totals().artists}
    return StepResult("music_ratings", counts=counts, lines=(f"[music_ratings] inserted {inserted} albums",
                                                              f"  artists: {counts['artists']}"))


def import_scrobbles(deps: MusicIngestDeps) -> StepResult:
    """Replace this dump's scrobbles with the file's plays. The dump is optional: absent, nothing is touched.

    At ~150k rows this is the biggest ingest, so artists and tracks are looked up in memory (the directory and a
    track cache) and the plays are written in one batch. Group or collaborative credits stay one artist each;
    membership is curated separately (`seed_artist_members`)."""
    music, inputs, locations = deps.music, deps.inputs, deps.locations
    path = locations.need("scrobbles_json", "SCROBBLES_JSON", "the scrobbles import")
    if not inputs.exists(path):
        return StepResult("scrobbles", lines=(f"[scrobbles] {path} not found, skipping",), counts={}, skipped=True)

    source_id = music.import_source(locations.scrobbles_json_stored)
    music.delete_scrobbles(source_id)
    scrobbles = list(music_ingest_rules.scrobbles_from_pages(inputs.read_json(path)))

    rules = load_artist_rules(inputs, locations)
    artists = ArtistIdentity(music, rules)
    before_artists = len(artists)
    artist_ids: Dict[str, r.ArtistId] = {}
    for artist, *_ in scrobbles:
        if artist not in artist_ids:
            artist_ids[artist] = artists.resolve(artist)
    new_artists = len(artists) - before_artists

    track_ids: Dict[Tuple[r.ArtistId, str, Optional[str]], r.TrackId] = {
        (artist_id, title, album): id_ for id_, artist_id, title, album in music.tracks()}
    before_tracks = len(track_ids)
    rows: List[r.NewScrobble] = []
    for artist, track, album, played_at in scrobbles:
        key = (artist_ids[artist], track, album)
        if key not in track_ids:
            track_ids[key] = music.add_track(*key)
        rows.append(r.NewScrobble(track_id=track_ids[key], played_at=played_at, import_source_id=source_id))
    new_tracks = len(track_ids) - before_tracks

    music.add_scrobbles(rows)
    deps.unit_of_work.commit()
    totals = music.totals()
    counts = {"parsed": len(scrobbles), "new_artists": new_artists, "new_tracks": new_tracks,
              "scrobbles": totals.scrobbles, "tracks": totals.tracks, "artists": totals.artists}
    return StepResult("scrobbles", counts=counts, lines=(
        f"[scrobbles] parsed {len(scrobbles)} scrobbles, {new_artists} new artists, {new_tracks} new tracks",
        f"  scrobbles: {counts['scrobbles']}, tracks: {counts['tracks']}, artists: {counts['artists']}"))


def seed_artist_members(deps: MusicIngestDeps) -> StepResult:
    """Record the curated group memberships of artist_rules.json as artist_members rows. Runs after the imports so
    a member already exists from their own credits; a group or member not yet known is created."""
    rules = load_artist_rules(deps.inputs, deps.locations)
    inserted = ArtistIdentity(deps.music, rules).seed_members()
    deps.unit_of_work.commit()
    return StepResult("seed_artist_members", counts={"artist_members": inserted},
                      lines=(f"[seed_artist_members] inserted {inserted} artist_members rows",))
