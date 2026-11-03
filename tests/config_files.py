"""Writing the TOML config file `paths.load()` reads, for tests that need a configuration of their own."""
import json
import os


def _value(v):
    """A TOML value for a str, a list of str or a bool. A JSON string is a valid TOML basic string."""
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(json.dumps(x) for x in v) + "]"
    return json.dumps(v)


def config_text(db_dir, data_dir, notes_vault=None, health_dir=None, client_sources=None, client_inputs=None, extra=""):
    """The text of a config file. `client_sources=None` leaves the key out (not declared); `extra` is appended as is."""
    lines = [f"db_dir = {_value(db_dir)}", f"data_dir = {_value(data_dir)}"]
    for key, value in (("notes_vault", notes_vault), ("health_dir", health_dir), ("client_sources", client_sources)):
        if value is not None:
            lines.append(f"{key} = {_value(value)}")
    if client_inputs:
        lines += ["", "[client_inputs]"] + [f"{name.lower()} = {_value(path)}" for name, path in client_inputs.items()]
    return "\n".join(lines) + "\n" + extra


def write_config(path, db_dir, data_dir, **settings):
    """Write a config file at `path` (its directory is created) and return `path`."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(config_text(db_dir, data_dir, **settings))
    return path


def write_unused_inputs_config(path, db_dir, data_dir, unused):
    """A config that defines every optional path (as `unused`), the way the CLI fixtures always have."""
    return write_config(path, db_dir, data_dir, notes_vault=unused, health_dir=unused,
                        client_inputs={n: unused for n in ("CONCERTS_CSV", "RYM_EXPORT_CSV", "SCROBBLES_JSON")})
