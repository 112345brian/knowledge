"""Where this person's data lives, as one immutable value.

Model ring: imports nothing else in the repo and does no I/O. An entry point reads `local_paths.py` once
(`paths.load()`), builds a `Locations`, and hands it down; nothing below an entry point looks configuration
up. The paths are already expanded (`~` resolved) by the loader; the properties here only join strings.
"""
import re
from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple

FACTS_FILENAME = "general_facts.json"
REVISIONS_FILENAME = "fact_revisions.jsonl"
PRIVACY_RULES_FILENAME = "privacy_rules.json"
ENTITIES_FILENAME = "entities.json"
SUBJECTS_FILENAME = "subjects.json"

HOME_ENV_VAR = "KNOWLEDGE_HOME"   # where `~` means, when it must differ from the OS user's home (see expand_home)


def expand_home(path, home):
    """`path` with a leading `~`, `~/x` or its backslash spelling resolved under `home`; anything else is returned unchanged.

    Pure: the caller supplies `home` (infrastructure reads HOME_ENV_VAR and the OS home). Only the *expanded*
    form changes with `home`; the form `local_paths.py` wrote (`~/MEGA/...`) is what the database stores.
    """
    if path == "~":
        return home
    if path[:2] in ("~/", "~\\"):
        return join_path(home, path[2:])
    return path


_WINDOWS_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|~\\)")


def canonical_path(path):
    """The OS-agnostic spelling of `path`, which is what the database stores: forward slashes.

    Only a path that is unmistakably Windows (drive letter, UNC, or `~` + backslash) is rewritten; anything else,
    including a POSIX name that contains a backslash, is returned unchanged. A POSIX path and an already
    canonical one are fixed points. `build_info.platform` says which OS a database was written on, so a
    canonical `C:/Users/x/v.md` is known to be a Windows path without being stored as one.
    """
    if path and _WINDOWS_ABSOLUTE.match(path):
        return path.replace("\\", "/")
    return path


def join_path(directory, *names):
    """`names` under `directory`, written with the separator `directory` already uses.

    Pure string work, so a POSIX fake (a "/data" directory) and a native Windows path (backslashes, a drive
    letter) both join correctly on any host: os.path.join would put a backslash into a fake's path on Windows,
    and a POSIX join would put a slash into a native one. A directory that mixes both is joined with `/`, which
    Windows accepts.
    """
    sep = "\\" if "\\" in directory and "/" not in directory else "/"
    out = directory
    for name in names:
        if sep == "\\":
            name = name.replace("/", "\\")
        out = name if not out else out.rstrip("/\\") + sep + name.lstrip("/\\")
    return out


@dataclass(frozen=True)
class Locations:
    """`private_data_dir` is the knowledge-private data directory; `knowledge_db_dir` holds the built db and its
    backups. `client_sources` names the optional client sources this checkout builds (what they are is the client
    directory's registration, `client_registry`); `client_inputs` holds every other setting `local_paths.py`
    defines, by its name (`MY_FEED_JSON`), which the registered sources read. A checkout that does not use a source
    leaves its settings out and the build never looks for them."""
    knowledge_db_dir: str
    private_data_dir: str
    notes_vault: Optional[str] = None
    health_dir: Optional[str] = None
    private_dir: Optional[str] = None   # the checkout local_paths.py came from; None for locations built by hand
    client_sources: Tuple[str, ...] = ()
    client_sources_declared: bool = True    # False: local_paths.py had no CLIENT_SOURCES (the registry may infer them)
    client_inputs: Mapping[str, str] = field(default_factory=dict)
    # `notes_vault` and `health_dir` as local_paths.py wrote them (`~/...`). Reads use the expanded fields above;
    # what the build compares against fact notes and stores in the database is the written form, so a database
    # does not carry one machine's home directory. None for locations built by hand: the field above is used.
    notes_vault_written: Optional[str] = None
    health_dir_written: Optional[str] = None
    client_inputs_written: Mapping[str, str] = field(default_factory=dict)   # the client settings as written

    def need(self, field, setting, for_what):
        """The value of an optional path, or a ValueError naming the `local_paths.py` setting to define."""
        value = getattr(self, field)
        if value is None:
            raise ValueError(f"{setting} is not set in local_paths.py; {for_what} needs it")
        return value

    def client_input(self, setting):
        """A client setting's path (expanded), or None when `local_paths.py` does not define it."""
        return self.client_inputs.get(setting)

    def need_input(self, setting, for_what):
        """A client setting's path, or a ValueError naming the `local_paths.py` setting to define."""
        value = self.client_inputs.get(setting)
        if value is None:
            raise ValueError(f"{setting} is not set in local_paths.py; {for_what} needs it")
        return value

    def client_input_stored(self, setting):
        """A client setting in the form the database stores (as written, forward slashes)."""
        return canonical_path(self.client_inputs_written.get(setting) or self.client_inputs.get(setting))

    @property
    def notes_vault_stored(self):
        """The notes vault in the form the database stores and fact notes cite."""
        return canonical_path(self.notes_vault_written or self.notes_vault)

    @property
    def health_dir_stored(self):
        """The health directory in the form the database stores."""
        return canonical_path(self.health_dir_written or self.health_dir)

    @property
    def db_dir(self):
        return self.knowledge_db_dir

    @property
    def db_path(self):
        return join_path(self.knowledge_db_dir, "knowledge.db")

    @property
    def backup_dir(self):
        return join_path(self.knowledge_db_dir, "backups")

    @property
    def data_dir(self):
        return self.private_data_dir

    @property
    def data_path(self):
        """The ad hoc facts file (`general_facts.json`)."""
        return join_path(self.private_data_dir, FACTS_FILENAME)

    def in_data_dir(self, name, data_dir=None):
        """`name` inside `data_dir` (default: this person's private data dir)."""
        return join_path(self.private_data_dir if data_dir is None else data_dir, name)

    def in_notes_vault(self, *names):
        """`names` under the (expanded) notes vault; a ValueError names the setting when no vault is configured."""
        return join_path(self.need("notes_vault", "NOTES_VAULT", "a vault path"), *names)

    def revisions_log(self, data_dir=None):
        return self.in_data_dir(REVISIONS_FILENAME, data_dir)

    def privacy_rules(self, data_dir=None):
        return self.in_data_dir(PRIVACY_RULES_FILENAME, data_dir)

    def entities_file(self, data_dir=None):
        return self.in_data_dir(ENTITIES_FILENAME, data_dir)

    def subjects_file(self, data_dir=None):
        return self.in_data_dir(SUBJECTS_FILENAME, data_dir)
