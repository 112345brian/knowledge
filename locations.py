"""Where this person's data lives, as one immutable value.

Model ring: imports nothing else in the repo and does no I/O. An entry point reads `local_paths.py` once
(`paths.load()`), builds a `Locations`, and hands it down; nothing below an entry point looks configuration
up. The paths are already expanded (`~` resolved) by the loader; the properties here only join strings.
"""
import posixpath
from dataclasses import dataclass
from typing import Optional, Tuple

FACTS_FILENAME = "general_facts.json"
REVISIONS_FILENAME = "fact_revisions.jsonl"
PRIVACY_RULES_FILENAME = "privacy_rules.json"
ENTITIES_FILENAME = "entities.json"
SUBJECTS_FILENAME = "subjects.json"


@dataclass(frozen=True)
class Locations:
    """`private_data_dir` is the knowledge-private data directory; `knowledge_db_dir` holds the built db and its
    backups. The optional client sources (see build_rules.CLIENT_SOURCES) name which of them this checkout builds
    and the input files they read; a checkout that does not use a source leaves its path None and the build never
    looks for it."""
    knowledge_db_dir: str
    private_data_dir: str
    notes_vault: Optional[str] = None
    health_dir: Optional[str] = None
    private_dir: Optional[str] = None   # the checkout local_paths.py came from; None for locations built by hand
    client_sources: Tuple[str, ...] = ()
    client_sources_implicit: bool = False
    concerts_csv: Optional[str] = None
    rym_export_csv: Optional[str] = None
    scrobbles_json: Optional[str] = None
    # `notes_vault` and `health_dir` as local_paths.py wrote them (`~/...`). Reads use the expanded fields above;
    # what the build compares against fact notes and stores in the database is the written form, so a database
    # does not carry one machine's home directory. None for locations built by hand: the field above is used.
    notes_vault_written: Optional[str] = None
    health_dir_written: Optional[str] = None

    def need(self, field, setting, for_what):
        """The value of an optional path, or a ValueError naming the `local_paths.py` setting to define."""
        value = getattr(self, field)
        if value is None:
            raise ValueError(f"{setting} is not set in local_paths.py; {for_what} needs it")
        return value

    @property
    def notes_vault_stored(self):
        """The notes vault in the form the database stores and fact notes cite."""
        return self.notes_vault_written or self.notes_vault

    @property
    def health_dir_stored(self):
        """The health directory in the form the database stores."""
        return self.health_dir_written or self.health_dir

    @property
    def db_dir(self):
        return self.knowledge_db_dir

    @property
    def db_path(self):
        return posixpath.join(self.knowledge_db_dir, "knowledge.db")

    @property
    def backup_dir(self):
        return posixpath.join(self.knowledge_db_dir, "backups")

    @property
    def data_dir(self):
        return self.private_data_dir

    @property
    def data_path(self):
        """The ad hoc facts file (`general_facts.json`)."""
        return posixpath.join(self.private_data_dir, FACTS_FILENAME)

    def in_data_dir(self, name, data_dir=None):
        """`name` inside `data_dir` (default: this person's private data dir)."""
        return posixpath.join(self.private_data_dir if data_dir is None else data_dir, name)

    def revisions_log(self, data_dir=None):
        return self.in_data_dir(REVISIONS_FILENAME, data_dir)

    def privacy_rules(self, data_dir=None):
        return self.in_data_dir(PRIVACY_RULES_FILENAME, data_dir)

    def entities_file(self, data_dir=None):
        return self.in_data_dir(ENTITIES_FILENAME, data_dir)

    def subjects_file(self, data_dir=None):
        return self.in_data_dir(SUBJECTS_FILENAME, data_dir)
