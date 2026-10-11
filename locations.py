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
    bodybuilding_vault: str
    health_dir: str
    client_sources: Tuple[str, ...] = ()
    client_sources_implicit: bool = False
    concerts_csv: Optional[str] = None
    rym_export_csv: Optional[str] = None
    scrobbles_json: Optional[str] = None

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
