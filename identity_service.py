"""The identity operations every build step shares: which source, which subject, which artist a name means.

Application ring: orchestrates the repositories (`build_repositories_port`) with the pure rules
(`identity_rules`); imports no adapter. Each class here is the ONE definition of its operation: a step that
finds-or-creates a subject, claims a citekey or resolves an artist does it through these, never with its own
SELECT/INSERT. They are constructed per step run, so their caches never survive a step (today's `State`/dict
caches are per run too).

Frozen semantics (tests/test_identity_service.py pins each against the legacy step behaviour):

    Source   citekeys() is a snapshot taken when asked; `claims()` is a snapshot set that a step grows as it
             inserts; an identifier already owned by ANOTHER source is recorded as a conflict and reported, never
             merged; an identifier the same source repeats is ignored.
    Subject  exact name first; the alias table is consulted only when asked (`via_aliases`); only then is a subject
             created, with the given domain or the schema default (`domain=None`). The cache is keyed by the name
             as given and remembers the id the first lookup produced.
    Artist   the key is `artist_key(canonical_artist_name(name, aliases))`; the cache is loaded once from the
             table (the first row of a case-variant group wins) and a created artist is added to it; nothing
             is ever evicted.
"""
from typing import Dict, List, Optional, Sequence, Tuple

import acquisition_rules
from artist_directory import ArtistDirectory
import build_records as r
import identity_rules
import inputs_port
import subject_rules
from build_repositories_port import MusicRepository, SourceRepository, SubjectRepository
from build_steps_port import Diagnostics


class SourceNotFound(RuntimeError):
    """A step needs a source that no earlier step created."""


class CitekeyClaims:
    """The citekeys that exist, as a set a step grows while it inserts: `claim` says whether a citekey is new."""
    def __init__(self, known):
        self._known = set(known)

    def claim(self, citekey) -> bool:
        if citekey in self._known:
            return False
        self._known.add(citekey)
        return True


class SourceIdentity:
    def __init__(self, sources: SourceRepository, diagnostics: Diagnostics):
        self._sources = sources
        self._diagnostics = diagnostics

    def citekeys(self) -> Dict[str, r.SourceId]:
        """citekey -> id as of now (a copy: later inserts do not show up in it)."""
        return dict(self._sources.citekeys())

    def claims(self) -> CitekeyClaims:
        return CitekeyClaims(self._sources.citekeys())

    def lookup(self, citekey: str) -> Optional[r.SourceId]:
        return self._sources.find(citekey)

    def require(self, citekey: str, hint: str) -> r.SourceId:
        """The id of the source with this citekey, or SourceNotFound ("source citekey not found: K (hint)")."""
        found = self._sources.find(citekey)
        if found is None:
            raise SourceNotFound(f"source citekey not found: {citekey} ({hint})")
        return found

    def attach_identifiers(self, source_id: r.SourceId, citekey: Optional[str],
                           pairs: Sequence[Tuple[str, str]]) -> List[r.IdentifierConflict]:
        """Record (scheme, value) pairs for a source, in order. A pair another source owns becomes a conflict row
        and a warning, and is returned; nothing is merged."""
        conflicts = []
        for scheme, value in pairs:
            owner = self._sources.identifier_owner(scheme, value)
            if owner is None:
                self._sources.add_identifier(source_id, scheme, value)
            elif owner.source_id != source_id:
                self._sources.record_identifier_conflict(source_id, scheme, value, owner.source_id)
                conflicts.append(r.IdentifierConflict(scheme, value, owner.citekey))
                self._diagnostics.emit(identity_rules.identifier_conflict_warning(scheme, value, owner.citekey, citekey))
        return conflicts

    def link_authors(self, source_id: r.SourceId, author_string: Optional[str]) -> None:
        for name, order in identity_rules.author_links(author_string):
            self._sources.link_author(source_id, name, order)

    def vault_file(self, path: Optional[str], inputs: inputs_port.Inputs) -> Optional[r.VaultFileId]:
        """The vault_files id for `path`, its fixity and custodial record written the first time a build sees it.
        A file that cannot be read is recorded as 'missing' by the inputs; this never raises for a bad path.
        None for an empty path."""
        if not path:
            return None
        state = self._sources.vault_file(path)
        if state is not None and state.file_state is not None:
            return state.id          # recorded this build: a file is hashed and read once, not once per fact
        file_id = state.id if state is not None else self._sources.add_vault_file(path)
        self._sources.record_vault_file_fixity(file_id, fixity_of(inputs, path))
        return file_id


def fixity_of(inputs: inputs_port.Inputs, path: str) -> r.FileFixity:
    """Fingerprint first, then the file attributes (a no-op off macOS), as the legacy helper does."""
    fp = inputs.fingerprint(path)
    acq = inputs.acquisition({k: None for k in acquisition_rules.COLUMNS}, path)
    return r.FileFixity(fp.get("content_sha256"), fp.get("size_bytes"), fp.get("file_mtime"), fp.get("mime_type"),
                        fp.get("file_state"), acq.get("acquired_at"), acq.get("acquired_via"), acq.get("where_from"),
                        acq.get("acquired_note"))


class SubjectIdentity:
    def __init__(self, subjects: SubjectRepository):
        self._subjects = subjects
        self._cache: Dict[str, r.SubjectId] = {}

    @property
    def touched(self) -> int:
        """How many distinct names this run has resolved (the measurements summary reports it)."""
        return len(self._cache)

    def resolve(self, name: str, *, domain: Optional[str] = None, via_aliases: bool = False) -> r.SubjectId:
        """The subject called `name`, created when it does not exist. With `via_aliases`, a name that is an alias
        resolves to its canonical subject before anything is created. `domain=None` leaves the new subject's
        domain to the schema default."""
        if name in self._cache:
            return self._cache[name]
        found, _via = subject_rules.identify(name, self._subjects.find_by_name,
                                             self._subjects.find_by_alias if via_aliases else (lambda _alias: None))
        subject_id = found if found is not None else self._subjects.add(name, domain)
        self._cache[name] = subject_id
        return subject_id


class ArtistIdentity(ArtistDirectory):
    """The artist directory plus the configured group memberships."""
    def __init__(self, music: MusicRepository, rules: r.ArtistRules):
        super().__init__(music, rules.aliases)
        self._rules = rules

    def seed_members(self) -> int:
        """Record the configured group memberships; returns how many rows were new. Run after the imports so the
        members already exist from their own credits."""
        inserted = 0
        for group_name, member_names in self._rules.members.items():
            group_id = self.resolve(group_name)
            for member_name in member_names:
                if self._music.add_artist_member(group_id, self.resolve(member_name)):
                    inserted += 1
        return inserted
