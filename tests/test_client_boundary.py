"""The base / client boundary: no module outside client/ imports client code.

The import-linter contract "The base (core and ingest) never imports a client source" (pyproject.toml) and tach's
deny-by-default allowlist enforce this on the module graph; this scan is the independent check, and it also sees what
those tools do not: a lazy import inside a function, an `importlib.import_module("client...")` or `__import__` call
with a literal client name, and a bare-stem import of a module that lives in client/ (which would only work by
accident of sys.path). A client module may import the base freely: only the reverse is checked.
"""
import ast
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(REPO, "client")


def _py_files(folder):
    for name in sorted(os.listdir(folder)):
        if name.endswith(".py"):
            yield os.path.join(folder, name)


def _client_stems():
    return {os.path.basename(p)[:-3] for p in _py_files(CLIENT) if not p.endswith("__init__.py")}


def _base_files():
    """Every module of the base: the repo root and ingest/ (tests are not modules of the product)."""
    found = list(_py_files(REPO))
    found += list(_py_files(os.path.join(REPO, "ingest")))
    return found


def client_imports(path, stems):
    """[(line, what)] for each import of client code in the file at `path`."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root == "client" or a.name in stems:
                    found.append((node.lineno, f"import {a.name}"))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            root = node.module.split(".")[0]
            if root == "client" or node.module in stems:
                found.append((node.lineno, f"from {node.module} import ..."))
            elif node.module == "ingest":
                pass
        elif isinstance(node, ast.Call):
            fn = ast.unparse(node.func)
            if fn in ("importlib.import_module", "import_module", "__import__") and node.args                     and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                name = node.args[0].value
                if name.split(".")[0] == "client" or name in stems:
                    found.append((node.lineno, f"{fn}({name!r})"))
    return found


def test_the_scan_sees_the_client_modules_and_the_base_modules():
    assert {"music_service", "music_store", "music_port", "music_records", "measurements_port",
            "measurements_records", "claims_port", "seed_claims"} <= _client_stems()
    base = {os.path.basename(p) for p in _base_files()}
    assert {"build_repositories_port.py", "build_records.py", "wiring.py", "container.py", "build.py"} <= base
    assert not any(n.startswith("music_") for n in base), sorted(n for n in base if n.startswith("music_"))


def test_no_non_client_module_imports_client_code():
    stems = _client_stems()
    offenders = [f"{os.path.relpath(p, REPO)}:{line}: {what}"
                 for p in _base_files() for line, what in client_imports(p, stems)]
    assert offenders == [], "the base must not import a client source: " + "; ".join(offenders)


def test_the_scan_catches_each_form_of_client_import(tmp_path):
    f = tmp_path / "bad.py"
    f.write_text(
        "import client.music_store\n"
        "from client import music_service\n"
        "import music_records\n"
        "def g():\n"
        "    from client.artist_rules import x\n"
        "    import importlib\n"
        "    importlib.import_module('client.concerts')\n"
        "    __import__('seed_claims')\n")
    got = client_imports(str(f), {"music_records", "seed_claims"})
    assert [line for line, _ in got] == [1, 2, 3, 5, 7, 8]


def test_the_core_build_contract_names_no_client_source():
    """build_records / build_repositories_port are the core's: sources, subjects, facts, revisions, privacy,
    entities and the unit of work."""
    import build_records
    import build_repositories_port as ports
    assert set(ports.REPOSITORY_PORTS) == {"sources", "subjects", "facts", "revisions", "privacy", "entities",
                                           "unit_of_work"}
    for name in ("ArtistId", "VenueId", "MetricId", "ClaimId", "NewMetric", "NewMeasurement", "MeasurementTotals",
                 "NewClaim", "NewAlbum", "NewScrobble", "ArtistRules", "NewAttendance", "MusicTotals"):
        assert not hasattr(build_records, name), f"build_records.{name} is client logic"
    assert not hasattr(ports.FactRepository, "link_measurements")
    for name in ("MusicRepository", "MeasurementRepository", "ClaimRepository"):
        assert not hasattr(ports, name), f"build_repositories_port.{name} is client logic"


def test_the_composition_seam_is_the_registry_not_an_import():
    """The base reaches a client step by the file path the client's bootstrap registers; no base module imports it."""
    import client_bootstrap
    client_steps = [path for path in client_bootstrap.registry().all_steps() if path.startswith("client/")]
    assert client_steps, "the registry lists the client steps"
    for path in client_steps:
        assert os.path.isfile(os.path.join(REPO, *path.split("/")))
