"""Reads where this person's data lives from the config file, once, at an entry point.

`load()` returns a `Locations` (see locations.py) and keeps nothing: no cache, no module-level names. Whoever
starts the program (the Typer app's `Container`, `build.py`, a tool's `main()`, a test) calls it and hands the
result down. The values and the fact/source data live outside this repository.

The file is TOML, found by the usual rule: `$KNOWLEDGE_CONFIG` if set, else `knowledge/config.toml` under
`$XDG_CONFIG_HOME` (default `~/.config`) on macOS and Linux and under the user's config directory on Windows. One
path on macOS and Linux keeps a dotfile manager's source layout the same on every machine. A named file that is
missing is an error, never a silent fallback, and so is a default location with no file.

Two things make one synced file serve several machines. Every path may start with `~`, which is resolved here per
machine. And a `config.local.toml` beside the file, if present, is merged over it, so a machine keeps its own
differences out of the synced copy. Any one setting can also be overridden by its `KNOWLEDGE_<NAME>` environment
variable (`KNOWLEDGE_DB_DIR`, ...); the main file must still exist, because a script the CLI starts re-reads it.
The settings, their types and the check that unknown keys are refused all come from `Settings` below.
"""
import os
import sys
import tomllib

import platformdirs
from pydantic import ValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, TomlConfigSettingsSource

import locations
from locations import Locations

CONFIG_ENV_VAR = locations.CONFIG_ENV_VAR
_APP_NAME = "knowledge"


class ConfigError(Exception):
    """The configuration is missing, unreadable or invalid. The message says which file and what to fix."""


class Settings(BaseSettings):
    """Every setting the file may hold. A name here is a TOML key and, upper-cased with a KNOWLEDGE_ prefix, the
    environment variable that overrides it. `client_inputs` is a table of the settings the registered client
    sources declare (`my_feed_json = "..."`); the base does not know them."""
    model_config = SettingsConfigDict(env_prefix="KNOWLEDGE_", extra="forbid", frozen=True)

    db_dir: str                                  # where knowledge.db and its backups live
    data_dir: str                                # the data checkout: facts, revisions, rules, entities
    notes_vault: str | None = None
    health_dir: str | None = None
    client_sources: tuple[str, ...] = ()         # the optional sources to build; none: the core only
    client_inputs: dict[str, str] = {}


def expand(path):
    """`path` with `~` resolved: under $KNOWLEDGE_HOME when that is set (a machine whose data lives outside the OS
    user's home, e.g. `~/MEGA` on another drive), else the OS user's home. Only the expanded form is affected."""
    home = os.environ.get(locations.HOME_ENV_VAR)
    return os.path.expanduser(locations.expand_home(path, home) if home else path)


def config_file():
    """The file `load()` reads: $KNOWLEDGE_CONFIG (an empty value counts as unset), else the per-user default."""
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        return os.path.abspath(expand(override))
    if sys.platform == "win32":
        directory = platformdirs.user_config_dir(_APP_NAME)
    else:     # the same place on macOS and Linux, where platformdirs would pick Library/Application Support for macOS
        directory = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), _APP_NAME)
    return os.path.join(directory, "config.toml")


def local_override_file(path):
    """The machine-local file merged over the config at `path`: `config.local.toml` beside `config.toml`."""
    root, extension = os.path.splitext(path)
    return f"{root}.local{extension}"


def _read(path):
    """The `Settings` in the TOML file at `path` with its local override merged over it and the environment over both;
    ConfigError if they can't be read."""
    class FromFile(Settings):
        @classmethod
        def settings_customise_sources(cls, settings_cls, init_settings: PydanticBaseSettingsSource,
                                       env_settings: PydanticBaseSettingsSource,
                                       dotenv_settings: PydanticBaseSettingsSource,
                                       file_secret_settings: PydanticBaseSettingsSource):
            # The environment, then the local file, then the main file: no .env file, no secrets directory. One source
            # per file because pydantic-settings merges sources key by key, but the files of one source only whole-table.
            return (init_settings, env_settings, TomlConfigSettingsSource(settings_cls, toml_file=local_override_file(path)),
                    TomlConfigSettingsSource(settings_cls, toml_file=path))

    try:
        return FromFile()
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} (or {local_override_file(path)}) is not valid TOML: {e}") from e
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
        raise ConfigError(f"{path} is not a valid configuration ({problems})") from e


def load():
    """Read the config file and return the `Locations` it describes."""
    path = config_file()
    if not os.path.isfile(path):
        source = f"{CONFIG_ENV_VAR}={os.environ[CONFIG_ENV_VAR]!r}" if os.environ.get(CONFIG_ENV_VAR) else "the default location"
        raise ConfigError(f"config file not found: {path} (from {source}). Create it with db_dir and data_dir, "
                          f"or set {CONFIG_ENV_VAR} to the file.")
    settings = _read(path)

    def expanded(value):
        return locations.canonical_path(expand(value)) if value else None

    # Client settings are upper-case names inside the base (`MY_FEED_JSON`), whatever case the file spells them in.
    client_inputs = {name.upper(): value for name, value in settings.client_inputs.items() if value}
    # Every path is canonical (forward slashes) from here down, so whatever the build stores is the same on every
    # OS; Windows opens such paths as they are. `*_written` keeps the file's own text.
    return Locations(
        knowledge_db_dir=expanded(settings.db_dir),
        private_data_dir=expanded(settings.data_dir),
        config_file=locations.canonical_path(path),
        notes_vault=expanded(settings.notes_vault), health_dir=expanded(settings.health_dir),
        client_sources=settings.client_sources,
        notes_vault_written=settings.notes_vault or None, health_dir_written=settings.health_dir or None,
        client_inputs={name: expanded(value) for name, value in client_inputs.items()},
        client_inputs_written=client_inputs,
    )
