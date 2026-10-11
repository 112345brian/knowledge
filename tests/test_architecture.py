"""Architecture enforcement (#36): tach module boundaries + import-linter contracts, run as tests.

Positive tests run both tools on the real tree. Negative tests prove every rule can fail: each
copies the real config (with the `#planned:` entries switched on, so the planned layout is
exercised too) into a temp dir holding an EMPTY module per configured module, adds one offending
import, and asserts the tool reports it. Real modules are never touched.

See README "Architecture enforcement" and tests/arch_check.py (why import-linter needs the
generated `kn` shadow package).
"""
import os
import re
import sys
import tomllib

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import arch_check as ac  # noqa: E402

REPO = ac.REPO
TACH_TOML = os.path.join(REPO, "tach.toml")
PYPROJECT = os.path.join(REPO, "pyproject.toml")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


PACKAGES = ("ingest", "client")   # the sub-packages: ETL core, optional client sources


def _bare(name):
    """`ingest.shared` / `client.concerts` -> `shared` / `concerts`: tach names a package's modules with the package,
    the import-linter shadow, the architecture map and the tests use the bare module name (unique across the repo)."""
    for package in PACKAGES:
        if name.startswith(package + "."):
            return name[len(package) + 1:]
    return name


def _tach_modules(text):
    return {_bare(m["path"]): m for m in tomllib.loads(text)["modules"]}


def _il_contracts(text):
    return {c["id"]: c for c in tomllib.loads(text)["tool"]["importlinter"]["contracts"]}


# --------------------------------------------------------------------- the real tree


def test_tach_check_passes():
    rc, out = ac.run_tach(REPO)
    assert rc == 0, "tach check failed (uv run tach check):\n" + out


def test_import_linter_passes():
    rc, out = ac.run_import_linter(REPO, PYPROJECT)
    assert rc == 0, "import-linter contracts broken (uv run python tests/arch_check.py):\n" + out


def test_git_only_runs_inside_private_git():
    problems = ac.scan_git_usage(REPO)
    assert problems == [], "git must only be run by private_git.py:\n" + "\n".join(problems)


MODEL = {"fact_rules", "timestamps", "validtime", "textmatch", "identifiers", "source_status", "entity_rules", "fact_state", "fact_entry", "locations", "pipeline_context", "fixity_rules", "acquisition_rules", "subject_rules"}  # the innermost ring: imports nothing else
PIPELINE_RULES = {"artist_rules", "music_ingest_rules", "measurement_rules", "fact_ingest_rules", "source_ingest_rules",
                  "backfill_rules", "build_rules", "normal_rules", "leak_rules"}  # pure, but only the pipeline (or one person) uses them
BUILD_TOOLING = {"normal_db", "leak_test", "leak_scan_store"}  # the normal-db builder and leak scanner: pipeline adapters
ONION_LAYERS = ("composition", "infrastructure", "application", "domain_services", "model")  # the library side of tach.toml, outermost first


def _package_modules():
    return {p: {f[:-3] for f in os.listdir(os.path.join(REPO, p)) if f.endswith(".py") and f != "__init__.py"} for p in PACKAGES}


def test_packages_hold_only_modules_tach_knows_under_their_package_name():
    """ingest/ (the core ETL) and client/ (optional sources) are packages. Each module is classified in tach.toml as
    `<package>.<name>`, and a bare name exists in only one place (the import-linter shadow is flat)."""
    raw = {m["path"] for m in tomllib.loads(_read(TACH_TOML))["modules"]}
    seen = {f[:-3] for f in os.listdir(REPO) if f.endswith(".py")}
    for package, names in _package_modules().items():
        assert names, package
        assert {f"{package}.{n}" for n in names} <= raw, sorted(f"{package}.{n}" for n in names - {m.split('.', 1)[1] for m in raw if m.startswith(package + '.')})
        assert not seen & names, f"a module exists in both the repo root and {package}/: {sorted(seen & names)}"
        seen |= names


def test_nothing_in_ingest_imports_the_client_package():
    """The core builds a complete knowledge.db without any client source: it must not depend on one."""
    import ast
    for name in _package_modules()["ingest"]:
        tree = ast.parse(_read(os.path.join(REPO, "ingest", name + ".py")))
        for node in ast.walk(tree):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            assert not any(m == "client" or m.startswith("client.") for m in mods), f"ingest/{name}.py imports {mods}"


def test_every_module_is_classified_in_both_configs():
    """A new top-level module must pick a layer in tach.toml AND appear in the import-linter
    contracts (tach's root_module=forbid catches the first; this catches the second)."""
    contracts = _il_contracts(_read(PYPROJECT))
    up = contracts["libraries-never-import-upward"]
    classified = {m.split(".", 1)[1] for m in up["source_modules"] + up["forbidden_modules"]}
    importable = set(ac.importable_stems(REPO))
    assert importable == classified, (
        f"modules missing from import-linter contracts: {sorted(importable - classified)}; "
        f"stale entries: {sorted(classified - importable)}")
    tach = _tach_modules(_read(TACH_TOML))
    assert importable <= set(tach), f"modules missing from tach.toml: {sorted(importable - set(tach))}"


@pytest.mark.parametrize("enable", [False, True], ids=["live", "with-planned"])
def test_configs_agree_on_what_a_library_is(enable):
    """The library list is written in three places (every tach layer below `pipeline`: composition, infrastructure,
    application, domain_services and model; and the two import-linter contracts); they must not drift apart."""
    t, p = _read(TACH_TOML), _read(PYPROJECT)
    if enable:
        t, p = ac.enable_planned(t), ac.enable_planned(p)
    tach_libs = {n for n, m in _tach_modules(t).items() if m["layer"] in ONION_LAYERS or n in PIPELINE_RULES or n in BUILD_TOOLING}
    c = _il_contracts(p)
    up = {m.split(".", 1)[1] for m in c["libraries-never-import-upward"]["source_modules"]}
    assert up == tach_libs
    ty = {m.split(".", 1)[1] for m in c["typer-only-in-knowledge"]["source_modules"]}
    serving = {n for n, m in _tach_modules(t).items() if m["layer"] == "serving"}
    assert ty == tach_libs | serving | {"cli_parity"}
    assert "libraries-layered" not in c, "ring order has one source of truth: tach.toml's `layers`"


def test_planned_entries_are_uncommented_once_the_module_exists():
    """Every `#planned:` line that mentions a not-yet-existing module must be activated when that
    module's file lands, so merging a module cannot silently skip its layer and import rules."""
    planned = {name for _, name in ac.planned_modules(_read(TACH_TOML))}
    landed = sorted(n for n in planned if n in ac.top_level_stems(REPO))
    stale = []
    for path in (TACH_TOML, PYPROJECT):
        for lineno, line in enumerate(_read(path).splitlines(), 1):
            if line.startswith(ac.PLANNED_PREFIX):
                for name in landed:
                    if re.search(r'"(?:kn\.)?' + name + r'"', line) or f'path = "{name}"' in line:
                        stale.append(f"{os.path.basename(path)}:{lineno}: {name}")
    assert not stale, (
        "module file now exists: delete the '#planned: ' prefix on these lines and set depends_on / "
        "layer to its real imports:\n" + "\n".join(stale))


# --------------------------------------------------------------------- negative tests


# The documented subprocess exceptions must exist in the tree or import-linter reports the
# ignore_imports line as unmatched (a stale exception is an error, as it should be).
BASELINE_CODE = {
    "private_git": "import subprocess\n",
    "script_runner": "import subprocess\n",
    "acquisition_store": "import subprocess\n",   # #46: the read-only macOS `xattr` reader
    # THE one CLI -> serving exception (launcher), exactly as written in the real cli_inbox.py.
    "cli_inbox": "import inbox  # tach-ignore inbox\n",
}


@pytest.fixture
def tree(tmp_path):
    """Synthetic project: real configs with planned entries ON, one empty file per tach module."""
    d = tmp_path / "proj"
    d.mkdir()
    tach_text = ac.enable_planned(_read(TACH_TOML))
    (d / "tach.toml").write_text(tach_text)
    (d / "pyproject.toml").write_text(ac.enable_planned(_read(PYPROJECT)))
    # Baseline = the maximal legal graph: every module imports exactly what tach.toml lets it
    # import (tach `exact = true` also rejects declared-but-unused dependencies), plus the
    # documented subprocess exceptions.
    for package in PACKAGES:
        (d / package).mkdir()
        (d / package / "__init__.py").write_text("")
    for name, mod in _tach_modules(tach_text).items():
        lines = [f"import {dep}" for dep in mod["depends_on"]]
        lines.append(BASELINE_CODE.get(name, ""))
        _module_path(d, name).write_text("\n".join(lines) + "\n")
    return d


def _check(tree, tool):
    if tool == "tach":
        return ac.run_tach(str(tree))
    return ac.run_import_linter(str(tree), str(tree / "pyproject.toml"))


_MODULE_DIR = {name: package for package, names in _package_modules().items() for name in names}


def _module_path(tree, module):
    """Where a synthetic module lives: in its package directory, or the root."""
    return tree / _MODULE_DIR.get(module, ".") / f"{module}.py"


def _offend(tree, module, code):
    p = _module_path(tree, module)
    p.write_text((p.read_text() if p.exists() else "") + code + "\n")


def test_baseline_full_config_passes_on_empty_tree(tree):
    """Planned entries enabled + an empty file per module: both tools pass. If this fails the
    planned lines themselves are malformed, and the negative tests below prove nothing."""
    for tool in ("tach", "import-linter"):
        rc, out = _check(tree, tool)
        assert rc == 0, f"{tool}:\n{out}"


# (id, module that gets the offending code, code, [(tool, [substrings expected in its output])])
CASES = [
    ("lib-imports-cli", "privacy", "import knowledge",
     [("tach", ["privacy", "knowledge"]), ("import-linter", ["kn.privacy", "kn.knowledge", "BROKEN"])]),
    ("lib-lazy-imports-cli", "privacy", "def f():\n    import knowledge",
     [("tach", ["privacy", "knowledge"]), ("import-linter", ["kn.privacy", "kn.knowledge", "BROKEN"])]),
    ("lib-imports-cli-registry", "clock", "from cli_parity import X",
     [("tach", ["clock", "cli_parity"]), ("import-linter", ["kn.clock", "kn.cli_parity"])]),
    ("lib-imports-serving", "modes", "import inbox",
     [("tach", ["modes", "inbox"]), ("import-linter", ["kn.modes", "kn.inbox"])]),
    ("lib-imports-pipeline", "modes", "import build",
     [("tach", ["modes", "build"]), ("import-linter", ["kn.modes", "kn.build"])]),
    ("lib-imports-typer", "clock", "import typer",
     [("import-linter", ["never import typer", "kn.clock", "typer", "BROKEN"])]),
    ("lib-imports-typer-lazy", "review", "def f():\n    import typer",
     [("import-linter", ["kn.review", "typer"])]),
    ("lib-imports-click", "paths", "from click import echo",
     [("import-linter", ["kn.paths", "click"])]),
    ("registry-imports-typer", "cli_parity", "import typer",
     [("import-linter", ["kn.cli_parity", "typer"])]),
    ("cli-imports-serving", "knowledge", "import inbox",
     [("tach", ["knowledge", "inbox"]), ("import-linter", ["never imports the serving layers", "kn.inbox"])]),
    ("cli-module-imports-serving", "cli_lifecycle", "import mcp_server",
     [("tach", ["cli_lifecycle", "mcp_server"]), ("import-linter", ["kn.cli_lifecycle", "kn.mcp_server"])]),
    ("serving-imports-pipeline", "inbox", "import build",
     [("tach", ["inbox", "build"]), ("import-linter", ["never imports the pipeline", "kn.build"])]),
    ("serving-imports-claims-audit", "inbox", "import claims_audit",
     [("tach", ["inbox", "claims_audit"]), ("import-linter", ["only through modes", "kn.claims_audit"])]),
    ("serving-imports-knowledge-queries", "mcp_server", "from knowledge import cmd_search",
     [("tach", ["mcp_server", "knowledge"]), ("import-linter", ["only through modes", "kn.knowledge"])]),
    ("serving-uses-raw-sqlite", "inbox", "import sqlite3",
     [("import-linter", ["only through modes", "sqlite3"])]),
    ("serving-imports-revisions", "mcp_server", "import revisions",
     [("tach", ["mcp_server", "revisions"]), ("import-linter", ["kn.mcp_server", "kn.revisions"])]),
    # ---- the inbox (real module, #36): it may import review and lifecycle and the stdlib, nothing else
    ("inbox-imports-revisions", "inbox", "import revisions",
     [("tach", ["inbox", "revisions"]), ("import-linter", ["only through modes", "kn.inbox", "kn.revisions"])]),
    ("inbox-imports-revisions-lazily", "inbox", "def f():\n    import revisions",
     [("tach", ["inbox", "revisions"]), ("import-linter", ["kn.inbox", "kn.revisions"])]),
    ("inbox-from-imports-revisions", "inbox", "from revisions import parse_timestamp",
     [("tach", ["inbox", "revisions"]), ("import-linter", ["kn.inbox", "kn.revisions"])]),
    ("inbox-imports-privacy", "inbox", "import privacy",
     [("tach", ["inbox", "privacy"]), ("import-linter", ["only through modes", "kn.inbox", "kn.privacy"])]),
    ("inbox-imports-the-git-helper", "inbox", "from private_git import commit_private_change",
     [("tach", ["inbox", "private_git"]), ("import-linter", ["only through modes", "kn.inbox", "kn.private_git"])]),
    ("inbox-imports-add-fact", "inbox", "import add_fact",
     [("tach", ["inbox", "add_fact"]), ("import-linter", ["kn.inbox", "kn.add_fact"])]),
    ("inbox-imports-facts-batch", "inbox", "import facts_batch",
     [("tach", ["inbox", "facts_batch"]), ("import-linter", ["kn.inbox", "kn.facts_batch"])]),
    ("inbox-imports-migrate-memory", "inbox", "import migrate_memory",
     [("tach", ["inbox", "migrate_memory"]), ("import-linter", ["kn.inbox", "kn.migrate_memory"])]),
    ("inbox-imports-normal-db", "inbox", "import normal_db",
     [("tach", ["inbox", "normal_db"]), ("import-linter", ["kn.inbox", "kn.normal_db"])]),
    ("inbox-lazy-sqlite", "inbox", "def f():\n    import sqlite3",
     [("import-linter", ["only through modes", "kn.inbox", "sqlite3"])]),
    ("inbox-imports-typer", "inbox", "import typer",
     [("import-linter", ["never import typer", "kn.inbox", "typer"])]),
    ("inbox-imports-the-cli-app", "inbox", "import knowledge",
     [("tach", ["inbox", "knowledge"]), ("import-linter", ["kn.inbox", "kn.knowledge"])]),
    ("inbox-imports-a-cli-module", "inbox", "import cli_lifecycle",
     [("tach", ["inbox", "cli_lifecycle"])]),
    ("inbox-imports-cli-inbox", "inbox", "import cli_inbox",
     [("tach", ["inbox", "cli_inbox"])]),
    # ---- the CLI -> serving exception is exactly the one launcher edge
    ("cli-lifecycle-imports-inbox", "cli_lifecycle", "import inbox",
     [("tach", ["cli_lifecycle", "inbox"]), ("import-linter", ["never imports the serving layers", "kn.cli_lifecycle", "kn.inbox"])]),
    ("cli-migrate-imports-inbox", "cli_migrate", "import inbox",
     [("tach", ["cli_migrate", "inbox"]), ("import-linter", ["kn.cli_migrate", "kn.inbox"])]),
    ("cli-facts-batch-imports-inbox-lazily", "cli_facts_batch", "def f():\n    import inbox",
     [("tach", ["cli_facts_batch", "inbox"]), ("import-linter", ["kn.cli_facts_batch", "kn.inbox"])]),
    ("cli-parity-imports-inbox", "cli_parity", "import inbox",
     [("tach", ["cli_parity", "inbox"]), ("import-linter", ["kn.cli_parity", "kn.inbox"])]),
    ("cli-inbox-imports-mcp-server", "cli_inbox", "import mcp_server",
     [("tach", ["cli_inbox", "mcp_server"]), ("import-linter", ["kn.cli_inbox", "kn.mcp_server"])]),
    ("cli-inbox-imports-the-pipeline", "cli_inbox", "import build",
     [("tach", ["cli_inbox", "build"])]),
    ("cli-inbox-imports-unlisted-library", "cli_inbox", "import revisions",
     [("tach", ["cli_inbox", "revisions"])]),
    # ---- libraries never import the new CLI / serving modules or each other upward
    ("lib-imports-cli-lifecycle", "review", "import cli_lifecycle",
     [("tach", ["review", "cli_lifecycle"]), ("import-linter", ["kn.review", "kn.cli_lifecycle", "BROKEN"])]),
    ("lib-imports-cli-migrate", "migrate_memory", "import cli_migrate",
     [("tach", ["migrate_memory", "cli_migrate"]), ("import-linter", ["kn.migrate_memory", "kn.cli_migrate"])]),
    ("lib-imports-cli-facts-batch", "facts_batch", "import cli_facts_batch",
     [("tach", ["facts_batch", "cli_facts_batch"]), ("import-linter", ["kn.facts_batch", "kn.cli_facts_batch"])]),
    ("lib-imports-cli-inbox", "lifecycle", "def f():\n    import cli_inbox",
     [("tach", ["lifecycle", "cli_inbox"]), ("import-linter", ["kn.lifecycle", "kn.cli_inbox"])]),
    ("lifecycle-imports-inbox", "lifecycle", "import inbox",
     [("tach", ["lifecycle", "inbox"]), ("import-linter", ["kn.lifecycle", "kn.inbox"])]),
    ("lifecycle-imports-pipeline", "lifecycle", "import build",
     [("tach", ["lifecycle", "build"]), ("import-linter", ["kn.lifecycle", "kn.build"])]),
    ("lifecycle-imports-typer", "lifecycle", "import typer",
     [("import-linter", ["kn.lifecycle", "typer"])]),
    ("lifecycle-subprocess", "lifecycle", "import subprocess",
     [("import-linter", ["kn.lifecycle", "subprocess"])]),
    ("lifecycle-imports-paths", "lifecycle", "import paths",
     [("tach", ["lifecycle", "paths"])]),
    ("review-imports-lifecycle-cycle", "review", "import lifecycle",
     [("tach", ["review", "lifecycle"])]),
    ("revisions-imports-lifecycle", "revisions", "import lifecycle",
     [("tach", ["revisions", "lifecycle"]), ("import-linter", ["kn.revisions", "kn.lifecycle"])]),
    ("lifecycle-imports-facts-batch-sibling", "lifecycle", "import facts_batch",
     [("tach", ["lifecycle", "facts_batch"])]),
    ("facts-batch-imports-lifecycle-sibling", "facts_batch", "import lifecycle",
     [("tach", ["facts_batch", "lifecycle"])]),
    ("migrate-memory-imports-facts-batch-sibling", "migrate_memory", "import facts_batch",
     [("tach", ["migrate_memory", "facts_batch"])]),
    ("modes-imports-lifecycle", "modes", "import lifecycle",
     [("tach", ["modes", "lifecycle"]), ("import-linter", ["kn.modes", "kn.lifecycle"])]),
    ("facts-batch-imports-revisions-unlisted", "facts_batch", "import revisions",
     [("tach", ["facts_batch", "revisions"])]),
    ("migrate-memory-imports-privacy-unlisted", "migrate_memory", "import privacy",
     [("tach", ["migrate_memory", "privacy"])]),
    # ---- cli_* modules stay inside their allowlists
    ("cli-lifecycle-imports-revisions", "cli_lifecycle", "import revisions",
     [("tach", ["cli_lifecycle", "revisions"])]),
    ("cli-migrate-imports-lifecycle", "cli_migrate", "import lifecycle",
     [("tach", ["cli_migrate", "lifecycle"])]),
    ("pipeline-imports-lifecycle", "build", "import lifecycle",
     [("tach", ["build", "lifecycle"])]),
    ("ingest-step-imports-inbox", "facts", "import inbox",
     [("tach", ["facts", "inbox"])]),
    ("paths-from-non-allowlisted-lib", "modes", "import paths",
     [("tach", ["modes", "paths"])]),
    ("paths-lazy-from-import", "claims_audit", "def f():\n    from paths import PRIVATE_DATA_DIR",
     [("tach", ["claims_audit", "paths"])]),
    ("paths-from-serving", "inbox", "import paths",
     [("tach", ["inbox", "paths"])]),
    ("paths-from-script-not-allowlisted", "seed_artist_members", "import paths",
     [("tach", ["seed_artist_members", "paths"])]),
    ("private-git-from-non-allowlisted-module", "claims_audit", "import private_git",
     [("tach", ["claims_audit", "private_git"])]),
    ("subprocess-in-library", "review", "import subprocess",
     [("import-linter", ["import subprocess", "kn.review", "subprocess"])]),
    ("subprocess-in-serving", "inbox", "import subprocess",
     [("import-linter", ["kn.inbox", "subprocess"])]),
    ("subprocess-lazy-in-pipeline", "build", "def f():\n    import subprocess",
     [("import-linter", ["kn.build", "subprocess"])]),
    ("library-cycle-via-upward-import", "revisions", "import review",
     [("tach", ["revisions", "review"])]),
    ("library-cycle-lazy", "private_git", "def f():\n    import add_fact",
     [("tach", ["private_git", "add_fact"])]),
    ("independent-siblings-import-each-other", "clock", "import paths",
     [("tach", ["clock", "paths"])]),
    ("lib-typechecking-import-of-cli", "modes", "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import knowledge",
     [("import-linter", ["kn.modes", "kn.knowledge"])]),
    ("ingest-step-imports-cli", "seed_sources", "import knowledge",
     [("tach", ["seed_sources", "knowledge"])]),
    ("ingest-step-imports-unlisted-library", "measurements", "import add_fact",
     [("tach", ["measurements", "add_fact"])]),
    # ---- onion: the domain (privacy) never touches infrastructure or its own adapter
    ("domain-imports-sqlite", "privacy", "import sqlite3",
     [("import-linter", ["never touch the file system", "kn.privacy", "sqlite3", "BROKEN"])]),
    ("domain-lazy-imports-os", "privacy", "def f():\n    import os",
     [("import-linter", ["kn.privacy", "os"])]),
    ("domain-imports-pathlib", "privacy", "from pathlib import Path",
     [("import-linter", ["kn.privacy", "pathlib"])]),
    ("domain-imports-subprocess", "privacy", "import subprocess",
     [("import-linter", ["kn.privacy", "subprocess"])]),
    ("domain-imports-its-adapter", "privacy", "import privacy_store",
     [("tach", ["privacy", "privacy_store"]), ("import-linter", ["kn.privacy", "kn.privacy_store"])]),
    ("domain-imports-clock", "privacy", "import clock",
     [("tach", ["privacy", "clock"]), ("import-linter", ["kn.privacy", "kn.clock"])]),
    ("domain-modes-imports-sqlite", "modes", "import sqlite3",
     [("import-linter", ["never touch the file system", "kn.modes", "sqlite3"])]),
    ("domain-modes-imports-its-adapter", "modes", "import modes_store",
     [("tach", ["modes", "modes_store"]), ("import-linter", ["kn.modes", "kn.modes_store"])]),
    ("model-imports-domain-service", "timestamps", "import privacy",
     [("tach", ["timestamps", "privacy"]), ("import-linter", ["kn.timestamps", "kn.privacy"])]),
    ("model-imports-application-service", "fact_rules", "import add_fact_service",
     [("tach", ["fact_rules", "add_fact_service"]), ("import-linter", ["kn.fact_rules", "kn.add_fact_service"])]),
    ("domain-modes-imports-revisions", "modes", "import revisions",
     [("tach", ["modes", "revisions"])]),   # revisions is a domain module now: only the tach allowlist stops it
    ("domain-claims-audit-imports-clock", "claims_audit", "import clock",
     [("tach", ["claims_audit", "clock"]), ("import-linter", ["kn.claims_audit", "kn.clock"])]),
    ("domain-claims-audit-imports-sqlite", "claims_audit", "import sqlite3",
     [("import-linter", ["kn.claims_audit", "sqlite3"])]),
    ("domain-timestamps-imports-os", "timestamps", "import os",
     [("import-linter", ["kn.timestamps", "os"])]),
    ("adapter-modes-store-imports-upward", "modes_store", "import lifecycle",
     [("tach", ["modes_store", "lifecycle"])]),
    ("serving-imports-claims-store", "inbox", "import claims_store",
     [("tach", ["inbox", "claims_store"]), ("import-linter", ["only through modes", "kn.claims_store"])]),
    ("domain-revisions-imports-sqlite", "revisions", "import sqlite3",
     [("import-linter", ["never touch the file system", "kn.revisions", "sqlite3"])]),
    ("domain-revisions-imports-os", "revisions", "def f():\n    import os",
     [("import-linter", ["kn.revisions", "os"])]),
    ("domain-revisions-imports-add-fact", "revisions", "import add_fact",
     [("tach", ["revisions", "add_fact"]), ("import-linter", ["kn.revisions", "kn.add_fact"])]),
    ("domain-revisions-imports-clock", "revisions", "import clock",
     [("tach", ["revisions", "clock"]), ("import-linter", ["kn.revisions", "kn.clock"])]),
    ("domain-revisions-imports-its-adapter", "revisions", "import revisions_store",
     [("tach", ["revisions", "revisions_store"]), ("import-linter", ["kn.revisions", "kn.revisions_store"])]),
    ("domain-review-rules-imports-review-store", "review_rules", "import review_store",
     [("tach", ["review_rules", "review_store"]), ("import-linter", ["kn.review_rules", "kn.review_store"])]),
    ("domain-review-rules-imports-subprocess", "review_rules", "import subprocess",
     [("import-linter", ["kn.review_rules", "subprocess"])]),
    ("domain-fact-rules-imports-sqlite", "fact_rules", "import sqlite3",
     [("import-linter", ["kn.fact_rules", "sqlite3"])]),
    ("adapter-revisions-store-imports-review", "revisions_store", "import review",
     [("tach", ["revisions_store", "review"])]),
    ("serving-imports-revisions-store", "inbox", "import revisions_store",
     [("tach", ["inbox", "revisions_store"]), ("import-linter", ["only through modes", "kn.revisions_store"])]),
    ("serving-imports-review-store", "inbox", "import review_store",
     [("tach", ["inbox", "review_store"]), ("import-linter", ["only through modes", "kn.review_store"])]),
    ("domain-new-fact-imports-sqlite", "new_fact", "import sqlite3",
     [("import-linter", ["never touch the file system", "kn.new_fact", "sqlite3"])]),
    ("domain-new-fact-imports-clock", "new_fact", "import clock",
     [("tach", ["new_fact", "clock"]), ("import-linter", ["kn.new_fact", "kn.clock"])]),
    ("domain-new-fact-imports-uuid", "new_fact", "import uuid",
     [("import-linter", ["kn.new_fact", "uuid"])]),
    ("domain-new-fact-imports-its-adapter", "new_fact", "import add_fact_store",
     [("tach", ["new_fact", "add_fact_store"]), ("import-linter", ["kn.new_fact", "kn.add_fact_store"])]),
    ("domain-lifecycle-rules-imports-sqlite", "lifecycle_rules", "import sqlite3",
     [("import-linter", ["kn.lifecycle_rules", "sqlite3"])]),
    ("domain-lifecycle-rules-imports-use-case", "lifecycle_rules", "import lifecycle",
     [("tach", ["lifecycle_rules", "lifecycle"]), ("import-linter", ["kn.lifecycle_rules", "kn.lifecycle"])]),
    ("domain-lifecycle-rules-imports-review", "lifecycle_rules", "import review",
     [("tach", ["lifecycle_rules", "review"]), ("import-linter", ["kn.lifecycle_rules", "kn.review"])]),
    ("adapter-add-fact-store-imports-upward", "add_fact_store", "import add_fact",
     [("tach", ["add_fact_store", "add_fact"])]),
    ("adapter-lifecycle-store-imports-lifecycle", "lifecycle_store", "import lifecycle",
     [("tach", ["lifecycle_store", "lifecycle"])]),
    ("serving-imports-lifecycle-store", "inbox", "import lifecycle_store",
     [("tach", ["inbox", "lifecycle_store"]), ("import-linter", ["only through modes", "kn.lifecycle_store"])]),
    ("serving-imports-add-fact-store", "inbox", "import add_fact_store",
     [("tach", ["inbox", "add_fact_store"]), ("import-linter", ["only through modes", "kn.add_fact_store"])]),
    ("domain-facts-batch-rules-imports-sqlite", "facts_batch_rules", "import sqlite3",
     [("import-linter", ["never touch the file system", "kn.facts_batch_rules", "sqlite3"])]),
    ("domain-facts-batch-rules-imports-use-case", "facts_batch_rules", "import facts_batch",
     [("tach", ["facts_batch_rules", "facts_batch"]), ("import-linter", ["kn.facts_batch_rules", "kn.facts_batch"])]),
    ("domain-migrate-memory-rules-imports-os", "migrate_memory_rules", "import os",
     [("import-linter", ["kn.migrate_memory_rules", "os"])]),
    ("domain-migrate-memory-rules-imports-clock", "migrate_memory_rules", "import clock",
     [("tach", ["migrate_memory_rules", "clock"]), ("import-linter", ["kn.migrate_memory_rules", "kn.clock"])]),
    ("domain-migrate-memory-rules-imports-glob", "migrate_memory_rules", "import glob",
     [("import-linter", ["kn.migrate_memory_rules", "glob"])]),
    ("adapter-migrate-memory-store-imports-use-case", "migrate_memory_store", "import migrate_memory",
     [("tach", ["migrate_memory_store", "migrate_memory"])]),
    ("domain-normal-rules-imports-sqlite", "normal_rules", "import sqlite3",
     [("import-linter", ["never touch the file system", "kn.normal_rules", "sqlite3"])]),
    ("domain-normal-rules-imports-tempfile", "normal_rules", "import tempfile",
     [("import-linter", ["kn.normal_rules", "tempfile"])]),
    ("domain-normal-rules-imports-adapter", "normal_rules", "import normal_db",
     [("tach", ["normal_rules", "normal_db"]), ("import-linter", ["kn.normal_rules", "kn.normal_db"])]),
    ("domain-leak-rules-imports-sqlite", "leak_rules", "import sqlite3",
     [("import-linter", ["kn.leak_rules", "sqlite3"])]),
    ("domain-leak-rules-imports-leak-test", "leak_rules", "import leak_test",
     [("tach", ["leak_rules", "leak_test"]), ("import-linter", ["kn.leak_rules", "kn.leak_test"])]),
    ("serving-imports-migrate-memory-store", "inbox", "import migrate_memory_store",
     [("tach", ["inbox", "migrate_memory_store"]), ("import-linter", ["only through modes", "kn.migrate_memory_store"])]),
    ("service-imports-adapter", "review_service", "import revisions_store",
     [("tach", ["review_service", "revisions_store"]), ("import-linter", ["never an adapter", "kn.review_service", "kn.revisions_store"])]),
    ("service-lazy-imports-adapter", "lifecycle_service", "def f():\n    import lifecycle_store",
     [("tach", ["lifecycle_service", "lifecycle_store"]), ("import-linter", ["kn.lifecycle_service", "kn.lifecycle_store"])]),
    ("service-imports-git-adapter", "lifecycle_service", "import private_git",
     [("tach", ["lifecycle_service", "private_git"]), ("import-linter", ["kn.lifecycle_service", "kn.private_git"])]),
    ("service-imports-its-facade", "add_fact_service", "import add_fact",
     [("tach", ["add_fact_service", "add_fact"]), ("import-linter", ["kn.add_fact_service", "kn.add_fact"])]),
    ("service-imports-clock", "facts_batch_service", "import clock",
     [("tach", ["facts_batch_service", "clock"]), ("import-linter", ["kn.facts_batch_service", "kn.clock"])]),
    ("service-imports-sqlite", "migrate_memory_service", "import sqlite3",
     [("import-linter", ["never an adapter", "kn.migrate_memory_service", "sqlite3"])]),
    ("service-imports-subprocess", "review_service", "def f():\n    import subprocess",
     [("import-linter", ["kn.review_service", "subprocess"])]),
    ("service-imports-uuid", "add_fact_service", "import uuid",
     [("import-linter", ["kn.add_fact_service", "uuid"])]),
    ("ids-adapter-imports-upward", "ids", "import add_fact",
     [("tach", ["ids", "add_fact"])]),
    ("cli-imports-sqlite", "knowledge", "import sqlite3",
     [("import-linter", ["reach infrastructure only through use cases", "kn.knowledge", "sqlite3"])]),
    ("cli-imports-subprocess", "knowledge", "def f():\n    import subprocess",
     [("import-linter", ["kn.knowledge", "subprocess"])]),
    ("cli-imports-os", "cli_lifecycle", "import os",
     [("import-linter", ["kn.cli_lifecycle", "os"])]),
    ("cli-imports-write-adapter", "cli_migrate", "import add_fact_store",
     [("tach", ["cli_migrate", "add_fact_store"]), ("import-linter", ["kn.cli_migrate", "kn.add_fact_store"])]),
    ("cli-imports-ids-adapter", "knowledge", "import ids",
     [("tach", ["knowledge", "ids"]), ("import-linter", ["kn.knowledge", "kn.ids"])]),
    ("cli-imports-clock", "cli_facts_batch", "import clock",
     [("tach", ["cli_facts_batch", "clock"]), ("import-linter", ["kn.cli_facts_batch", "kn.clock"])]),
    ("inbox-imports-os", "inbox", "import os",
     [("import-linter", ["kn.inbox", "os"])]),
    ("serving-imports-fact-queries", "inbox", "import fact_queries",
     [("tach", ["inbox", "fact_queries"]), ("import-linter", ["only through modes", "kn.fact_queries"])]),
    ("subprocess-in-the-cli-app", "knowledge", "import subprocess",
     [("import-linter", ["import subprocess", "kn.knowledge", "subprocess"])]),
    # ---- a store never reaches back into a use case (the dependency points inward, never outward)
    ("store-imports-a-service", "revisions_store", "import review_service",
     [("tach", ["revisions_store", "review_service"])]),
    ("store-imports-its-own-service", "lifecycle_store", "import lifecycle_service",
     [("tach", ["lifecycle_store", "lifecycle_service"])]),
    ("store-imports-rules-edit-service", "privacy_store", "import rules_edit_service",
     [("tach", ["privacy_store", "rules_edit_service"])]),
    ("store-imports-the-rules-edit-facade", "privacy_store", "import rules_edit",
     [("tach", ["privacy_store", "rules_edit"])]),
    ("lock-adapter-imports-a-store", "locks", "import add_fact_store",
     [("tach", ["locks", "add_fact_store"])]),
    # ---- knowledge composes nothing itself: no store, no git (rules go through the rules_edit facade)
    ("knowledge-imports-privacy-store", "knowledge", "import privacy_store",
     [("tach", ["knowledge", "privacy_store"]), ("import-linter", ["reach infrastructure only through use cases", "kn.knowledge", "kn.privacy_store"])]),
    ("knowledge-imports-private-git", "knowledge", "import private_git",
     [("tach", ["knowledge", "private_git"]), ("import-linter", ["reach infrastructure only through use cases", "kn.knowledge", "kn.private_git"])]),
    ("knowledge-imports-revisions-store", "knowledge", "import revisions_store",
     [("tach", ["knowledge", "revisions_store"]), ("import-linter", ["reach infrastructure only through use cases", "kn.knowledge", "kn.revisions_store"])]),
    ("knowledge-imports-rules-edit-service", "knowledge", "import rules_edit_service",
     [("tach", ["knowledge", "rules_edit_service"])]),
    ("rules-edit-service-imports-adapter", "rules_edit_service", "import privacy_store",
     [("tach", ["rules_edit_service", "privacy_store"]), ("import-linter", ["never an adapter", "kn.rules_edit_service", "kn.privacy_store"])]),
    ("script-runner-imports-upward", "script_runner", "import knowledge",
     [("tach", ["script_runner", "knowledge"]), ("import-linter", ["kn.script_runner", "kn.knowledge"])]),
    ("domain-artist-rules-imports-sqlite", "artist_rules", "import sqlite3",
     [("import-linter", ["never touch the file system", "kn.artist_rules", "sqlite3"])]),
    ("domain-fact-ingest-rules-imports-os", "fact_ingest_rules", "import os",
     [("import-linter", ["kn.fact_ingest_rules", "os"])]),
    ("domain-fact-ingest-rules-imports-add-fact", "fact_ingest_rules", "import add_fact",
     [("tach", ["fact_ingest_rules", "add_fact"]), ("import-linter", ["kn.fact_ingest_rules", "kn.add_fact"])]),
    ("domain-source-ingest-rules-imports-glob", "source_ingest_rules", "import glob",
     [("import-linter", ["kn.source_ingest_rules", "glob"])]),
    ("domain-music-ingest-rules-imports-pathlib", "music_ingest_rules", "from pathlib import Path",
     [("import-linter", ["kn.music_ingest_rules", "pathlib"])]),
    ("domain-measurement-rules-imports-sqlite", "measurement_rules", "import sqlite3",
     [("import-linter", ["kn.measurement_rules", "sqlite3"])]),
    ("domain-measurement-rules-imports-paths", "measurement_rules", "import paths",
     [("tach", ["measurement_rules", "paths"]), ("import-linter", ["kn.measurement_rules", "kn.paths"])]),
    ("domain-build-rules-imports-shutil", "build_rules", "import shutil",
     [("import-linter", ["kn.build_rules", "shutil"])]),
    ("domain-build-rules-imports-clock", "build_rules", "import clock",
     [("tach", ["build_rules", "clock"]), ("import-linter", ["kn.build_rules", "kn.clock"])]),
    ("domain-backfill-rules-imports-lock", "backfill_rules", "import add_fact_store",
     [("tach", ["backfill_rules", "add_fact_store"]), ("import-linter", ["kn.backfill_rules", "kn.add_fact_store"])]),
    ("ingest-script-imports-a-store", "concerts", "import revisions_store",
     [("tach", ["concerts", "revisions_store"])]),
    ("build-imports-a-use-case", "build", "import review_service",
     [("tach", ["build", "review_service"])]),
    ("adapter-imports-upward", "privacy_store", "import add_fact",
     [("tach", ["privacy_store", "add_fact"])]),
    ("serving-imports-privacy-adapter", "inbox", "import privacy_store",
     [("tach", ["inbox", "privacy_store"]), ("import-linter", ["only through modes", "kn.privacy_store"])]),
]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_violation_is_reported(tree, case):
    _id, module, code, expectations = case
    _offend(tree, module, code)
    for tool, needles in expectations:
        rc, out = _check(tree, tool)
        assert rc != 0, f"{tool} did not fail for {_id}:\n{out}"
        for n in needles:
            assert n in out, f"{tool} output for {_id} lacks {n!r}:\n{out}"


def _repo_py_files():
    return [p for _, p in sorted(ac._source_files(REPO).items())]


def test_the_only_tach_ignore_is_the_cli_inbox_launcher_import():
    """Inventory of source-level exceptions: tach's inline `# tach-ignore` is how the one
    CLI -> serving edge is allowed, so there must be exactly that one, naming only `inbox`."""
    found = []
    for path in _repo_py_files():
        for line in _read(path).splitlines():
            if "tach-ignore" in line:
                found.append((os.path.basename(path), line.strip()))
    assert found == [("cli_inbox.py", "import inbox  # tach-ignore inbox")], found
    assert "tach-ignore" not in _read(TACH_TOML).replace("`# tach-ignore`", "").replace("# tach-ignore inbox", "")


def test_exception_lists_are_exactly_the_documented_ones():
    """Every ignore_imports line is an exception someone has to justify; this pins the full list."""
    contracts = _il_contracts(_read(PYPROJECT))
    ignores = {cid: c.get("ignore_imports", []) for cid, c in contracts.items() if c.get("ignore_imports")}
    assert ignores == {
        "cli-never-imports-serving": ["kn.cli_inbox -> kn.inbox"],
        "subprocess-allowlist": ["kn.private_git -> subprocess", "kn.script_runner -> subprocess", "kn.acquisition_store -> subprocess"],
    }, ignores
    assert "ignore_imports" not in contracts["domain-has-no-infrastructure"]
    assert "ignore_imports" not in contracts["use-cases-depend-on-ports"]
    assert "ignore_imports" not in contracts["serving-reads-facts-through-modes"]
    assert "ignore_imports" not in contracts["serving-never-imports-pipeline"]


def test_domain_list_is_a_ratchet_and_the_domain_is_pure_at_the_source_level():
    """Hexagonal architecture: the domain list may grow but never shrink or gain an exception.
    Each domain module must also parse to stdlib-only imports with no open()/print-to-disk calls,
    which catches what import-linter cannot see (importlib, __import__, builtins.open)."""
    import ast
    domain = {m.split(".", 1)[1] for m in _il_contracts(_read(PYPROJECT))["domain-has-no-infrastructure"]["source_modules"]}
    assert {"privacy", "modes", "claims_audit", "timestamps", "revisions", "review_rules", "fact_rules", "new_fact", "lifecycle_rules",
               "facts_batch_rules", "migrate_memory_rules", "normal_rules", "leak_rules",
               "artist_rules", "fact_ingest_rules", "source_ingest_rules", "music_ingest_rules",
               "measurement_rules", "build_rules", "backfill_rules"} <= domain, "a module was removed from the domain list; migrate it, do not drop it"
    for name in sorted(domain):
        tree = ast.parse(_read(ac._source_files(REPO)[name]))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"open", "__import__", "exec", "eval"}, f"{name}.py:{node.lineno} calls {node.func.id}()"
                assert node.func.id != "print", f"{name}.py:{node.lineno}: the domain does not print"
            if isinstance(node, ast.Attribute) and node.attr in {"import_module"}:
                raise AssertionError(f"{name}.py:{node.lineno}: dynamic import in the domain")


def test_use_case_list_is_pinned_and_matches_the_ports_test():
    """The *_service modules are the use cases; each must be under the use-cases-depend-on-ports contract."""
    contracts = _il_contracts(_read(PYPROJECT))
    listed = {m.split(".", 1)[1] for m in contracts["use-cases-depend-on-ports"]["source_modules"]}
    on_disk = {n for n in ac.importable_stems(REPO) if n.endswith("_service")}
    assert listed == on_disk, f"a *_service module is outside the contract: {sorted(on_disk ^ listed)}"
    assert listed == {"review_service", "lifecycle_service", "add_fact_service", "facts_batch_service",
                      "migrate_memory_service", "rules_edit_service", "entities_service", "subjects_service", "queries_service", "scripts_service"}


def _architecture_roles():
    roles = {}
    for line in _read(os.path.join(REPO, "docs", "architecture.md")).splitlines():
        m = re.match(r"^\| `([A-Za-z0-9_]+)` \| ([^|]+?) \|", line)
        if m:
            assert m.group(1) not in roles, f"{m.group(1)} is listed twice in docs/architecture.md"
            roles[m.group(1)] = m.group(2)
    return roles


def test_architecture_map_classifies_every_module_and_matches_the_contracts():
    """docs/architecture.md says what each module is; it must not drift from the code or the contracts."""
    roles = _architecture_roles()
    on_disk = set(ac.top_level_stems(REPO))
    assert set(roles) == on_disk, f"unclassified: {sorted(on_disk - set(roles))}; stale: {sorted(set(roles) - on_disk)}"
    contracts = _il_contracts(_read(PYPROJECT))
    domain = {m.split(".", 1)[1] for m in contracts["domain-has-no-infrastructure"]["source_modules"]}
    services = {m.split(".", 1)[1] for m in contracts["use-cases-depend-on-ports"]["source_modules"]}
    assert {n for n, r in roles.items() if r in ("model", "domain service", "pipeline rules")} == domain
    assert {n for n, r in roles.items() if r == "pipeline rules"} == PIPELINE_RULES
    assert {n for n, r in roles.items() if r == "model"} == MODEL
    assert {n for n, r in roles.items() if r == "application service"} == services
    assert {n for n, r in roles.items() if r == "composition root"} == {"review", "lifecycle", "add_fact", "facts_batch", "migrate_memory", "rules_edit", "entities", "subjects", "queries", "scripts", "container"}
    driving = {m.split(".", 1)[1] for m in contracts["driving-adapters-no-infrastructure"]["source_modules"]}
    assert driving <= {n for n, r in roles.items() if r == "driving adapter"}
    allowed = {"model", "domain service", "application service", "infrastructure", "composition root", "composition helper", "pipeline rules",
               "driving adapter", "ETL adapter (driving, batch)"}
    assert set(roles.values()) <= allowed


def test_tach_layers_are_the_onion():
    """tach.toml's layers are the onion's rings, outermost first, and each module sits in the layer its role in
    docs/architecture.md names. `application` is inside `infrastructure`, so an application service importing
    an adapter is a layer violation, not just a missing allowlist entry; `model` is innermost."""
    cfg = _read(TACH_TOML)
    layers = re.search(r"^layers = \[(.*?)\]", cfg, re.M).group(1)
    assert [x.strip().strip('"') for x in layers.split(",")] == [
        "serving", "cli", "pipeline", "composition", "infrastructure", "application", "domain_services", "model"]
    expected = {"model": "model", "domain service": "domain_services", "application service": "application",
                "infrastructure": "infrastructure", "composition root": "composition",
                "ETL adapter (driving, batch)": "pipeline", "pipeline rules": "pipeline",
                "composition helper": "composition"}
    tach = _tach_modules(cfg)
    for name, role in _architecture_roles().items():
        layer = tach[name]["layer"]
        if role == "driving adapter":
            assert layer in ("cli", "serving"), f"{name} is a driving adapter but tach has it in {layer}"
        else:
            assert layer == expected[role], f"{name} is a {role} but tach has it in {layer}"


def test_model_ring_is_pinned_and_the_contract_forbids_every_other_module():
    """The model is the onion's centre: tach's `model` layer, the import-linter contract's sources and the
    pinned MODEL set must agree, and the contract must forbid every module outside it (a new module is
    caught by test_every_module_is_classified_in_both_configs first, then has to be added here)."""
    contract = _il_contracts(_read(PYPROJECT))["model-is-innermost"]
    sources = {m.split(".", 1)[1] for m in contract["source_modules"]}
    forbidden = {m.split(".", 1)[1] for m in contract["forbidden_modules"]}
    tach_model = {n for n, m in _tach_modules(_read(TACH_TOML)).items() if m["layer"] == "model"}
    assert sources == tach_model == MODEL
    assert forbidden == set(ac.importable_stems(REPO)) - MODEL
    assert "ignore_imports" not in contract


def test_driving_adapters_are_forbidden_every_infrastructure_module_but_the_script_launcher():
    """The outermost ring reaches data only through composition roots: the contract must name every module
    tach puts in the infrastructure layer, with no exception."""
    contract = _il_contracts(_read(PYPROJECT))["driving-adapters-no-infrastructure"]
    forbidden = {m.split(".", 1)[1] for m in contract["forbidden_modules"] if m.startswith("kn.")}
    infra = {n for n, m in _tach_modules(_read(TACH_TOML)).items()
             if m["layer"] == "infrastructure" and "." not in n}
    assert infra <= forbidden, f"driving adapters may import these infrastructure modules: {sorted(infra - forbidden)}"


ENTRY_POINTS = {"container", "build", "normal_db", "backfill_dates", "backfill_source_keys", "backfill_extracted_hashes",
                "backfill_snapshot_date", "export_music_taste", "clean_concerts_csv"}


def test_only_entry_points_read_the_configuration():
    """`paths` is how configuration is read. Only the places a process starts may import it; everything else is
    handed a Locations. tach's allowlist is the enforcement, this pins who is on it."""
    importers = {n for n, m in _tach_modules(_read(TACH_TOML)).items() if "paths" in m.get("depends_on", [])}
    assert importers == ENTRY_POINTS, f"unexpected: {sorted(importers - ENTRY_POINTS)}; gone: {sorted(ENTRY_POINTS - importers)}"


def _all_source_trees():
    import ast
    for name, path in sorted(ac._source_files(REPO).items()):
        yield name, ast.parse(_read(path))


def test_no_hidden_global_state_in_the_application():
    """No `global` statement, no memoised function (a cache is hidden state; textmatch's regex compile cache is
    a pure function of its argument), no module-level mutable container (a cache, a registry) in any module."""
    import ast
    allowed_cache = {"textmatch"}
    offenders = []
    for name, tree in _all_source_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Global):
                offenders.append(f"{name}.py:{node.lineno}: global {', '.join(node.names)}")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in node.decorator_list:
                    target = d.func if isinstance(d, ast.Call) else d
                    text = ast.unparse(target)
                    if text.endswith(("lru_cache", "functools.cache", "cache")) and name not in allowed_cache:
                        offenders.append(f"{name}.py:{node.lineno}: memoised {node.name}()")
        for node in tree.body:
            if isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                value = node.value
                empty_mutable = (isinstance(value, (ast.Dict, ast.List, ast.Set)) and not
                                 (getattr(value, "keys", None) or getattr(value, "elts", None))) or \
                    (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                     and value.func.id in ("dict", "list", "set", "defaultdict") and not value.args)
                if empty_mutable and names and not all(n.isupper() or n == "__all__" for n in names):
                    offenders.append(f"{name}.py:{node.lineno}: module-level mutable {names}")
                if any(n in ("DEPS", "PORTS") for n in names):
                    offenders.append(f"{name}.py:{node.lineno}: module-level {names} (compose() wires; nothing is built at import)")
    assert offenders == [], "\n".join(offenders)


def test_build_steps_import_no_infrastructure_module():
    """Everything a step touches outside its connection arrives on its context (`ctx.inputs`, `ctx.stores`): it
    imports no module from tach's infrastructure layer, only pure rules and other steps' helpers."""
    mods = _tach_modules(_read(TACH_TOML))
    infra = {n for n, m in mods.items() if m["layer"] == "infrastructure"}
    steps = [n for n, m in mods.items() if m["layer"] == "pipeline" and n not in ENTRY_POINTS
             and (n.startswith(("seed_", "link_", "apply_", "literature_", "facts", "concerts", "measurements", "scrobbles",
                                "music_", "shared", "snapshot_"))
                  or n in ("facts", "concerts", "shared"))]
    assert steps, "found no step modules"
    bad = {n: sorted(set(mods[n].get("depends_on", [])) & infra) for n in steps if set(mods[n].get("depends_on", [])) & infra}
    assert bad == {}, f"steps importing infrastructure: {bad}"


def test_raw_file_and_database_io_lives_only_in_adapters():
    """Every call that touches the disk, a sqlite file or a process (open, os file ops, sqlite3.connect, shutil,
    subprocess, tempfile) is in an infrastructure module. Nothing else does any: not the model, domain, application,
    composition, CLI, serving or step code, not the maintenance tools, not build.py, and not the normal-db builder
    and leak scanner (normal_db, leak_test, leak_scan_store), which work through a BuildFiles."""
    import ast
    banned = {"open", "glob.glob", "glob.iglob", "os.listdir", "os.scandir", "os.walk", "os.stat", "os.makedirs",
              "os.mkdir", "os.remove", "os.rename", "os.replace", "os.unlink", "os.path.exists", "os.path.isfile",
              "os.path.isdir", "os.path.getsize", "os.path.getmtime", "sqlite3.connect", "shutil.copy", "shutil.copy2",
              "shutil.move", "shutil.rmtree", "subprocess.run", "subprocess.call", "subprocess.Popen",
              "tempfile.gettempdir", "tempfile.TemporaryDirectory", "os.fdopen", "os.open", "Path"}
    layer = {n: m["layer"] for n, m in _tach_modules(_read(TACH_TOML)).items()}
    offenders = []
    for name, tree in _all_source_trees():
        if layer.get(name) == "infrastructure":
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func) in banned:
                offenders.append(f"{name}.py ({layer.get(name)}):{node.lineno}: {ast.unparse(node.func)}()")
    assert offenders == [], "\n".join(offenders)


def test_maintenance_tools_do_no_file_or_database_io_of_their_own():
    """The one-off tools (backfills, the exports) take a DataFiles / Inputs and read configuration once in main():
    they open no file and no sqlite database themselves."""
    import ast
    banned = {"open", "glob.glob", "os.listdir", "os.walk", "os.stat", "os.makedirs", "os.remove", "os.replace",
              "os.path.exists", "os.path.isfile", "os.path.isdir", "sqlite3.connect", "shutil.copy", "subprocess.run"}
    tools = {"backfill_dates", "backfill_source_keys", "backfill_extracted_hashes", "backfill_snapshot_date",
             "export_music_taste", "clean_concerts_csv"}
    offenders = []
    for name, tree in _all_source_trees():
        if name not in tools:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func) in banned:
                offenders.append(f"{name}.py:{node.lineno}: {ast.unparse(node.func)}()")
    assert offenders == [], "\n".join(offenders)


def test_build_steps_do_no_file_or_database_io_of_their_own():
    """A step reads through `ctx.inputs` (inputs_port) and writes through the connection it is given. It opens no
    file, globs no directory, checks no path on disk and opens no sqlite file itself; the entry-point tools
    (backfills, the exports) are the outer ring's own scripts and are not steps."""
    import ast
    banned_calls = {"open", "glob.glob", "glob.iglob", "os.listdir", "os.scandir", "os.walk", "os.stat", "os.makedirs",
                    "os.mkdir", "os.remove", "os.rename", "os.replace", "os.path.exists", "os.path.isfile",
                    "os.path.isdir", "os.path.getsize", "os.path.getmtime", "sqlite3.connect", "shutil.copy",
                    "shutil.move", "shutil.rmtree", "subprocess.run", "subprocess.call", "Path"}
    steps, offenders = [], []
    for name, tree in _all_source_trees():
        if name in ENTRY_POINTS or name.endswith("_rules"):
            continue
        path = ac._source_files(REPO)[name]
        if os.path.relpath(path, REPO).split(os.sep)[0] not in ("ingest", "client"):
            continue
        steps.append(name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func) in banned_calls:
                offenders.append(f"{name}.py:{node.lineno}: {ast.unparse(node.func)}()")
    assert steps, "found no step modules"
    assert offenders == [], "\n".join(offenders)


def test_the_clock_has_no_ambient_value():
    """Time is a Clock object handed to whoever needs it: no module-level now()/frozen(), no ContextVar."""
    text = _read(os.path.join(REPO, "clock.py"))
    assert "contextvars" not in text and "global " not in text
    import clock
    assert not any(hasattr(clock, n) for n in ("now", "now_iso", "frozen"))


def test_application_services_import_no_system_module():
    """An application service gets every path from Locations and every effect from a port: it imports no os, sys,
    pathlib, subprocess or sqlite3 at all (the earlier rule allowed `os.path` string helpers)."""
    import ast
    forbidden = {"os", "sys", "posixpath", "ntpath", "pathlib", "subprocess", "sqlite3", "shutil", "tempfile", "glob", "io"}
    offenders = []
    for name, tree in _all_source_trees():
        if not name.endswith("_service"):
            continue
        for node in ast.walk(tree):
            mods = [a.name.split(".")[0] for a in node.names] if isinstance(node, ast.Import) else \
                [(node.module or "").split(".")[0]] if isinstance(node, ast.ImportFrom) else []
            for m in mods:
                if m in forbidden:
                    offenders.append(f"{name}.py:{node.lineno}: import {m}")
    assert offenders == [], "\n".join(offenders)


def test_composition_roots_import_only_what_they_use():
    """A composition root binds services to adapters and publishes its own functions. It must not re-export
    other modules' names (that hides where a name lives and lets callers reach infrastructure through it):
    every name a composition module imports has to be used in that module."""
    import ast
    roles = _architecture_roles()
    for name in sorted(n for n, r in roles.items() if r == "composition root"):
        tree = ast.parse(_read(os.path.join(REPO, name + ".py")))
        imported = {}
        for node in tree.body:
            if isinstance(node, ast.Import):
                for a in node.names:
                    imported[(a.asname or a.name).split(".")[0]] = node.lineno
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    imported[a.asname or a.name] = node.lineno
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        unused = {k: v for k, v in imported.items() if k not in used}
        assert not unused, f"{name}.py re-exports instead of using: {unused}"


def test_inbox_imports_only_pure_rule_modules_and_the_standard_library():
    """Direct check on the real file, independent of both tools: serving holds no write logic."""
    import ast
    tree = ast.parse(_read(os.path.join(REPO, "inbox.py")))
    local = set(ac.importable_stems(REPO))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported & local == {"fact_rules", "review_rules"}, imported & local   # pure inner-ring helpers; review and lifecycle arrive as `app`
    assert "sqlite3" not in imported


def test_unregistered_module_is_reported_by_tach(tree):
    """root_module = forbid: a new .py that is not in tach.toml fails, so adding a module forces
    choosing its layer and allowed imports."""
    (tree / "brand_new.py").write_text("import privacy\n")
    rc, out = _check(tree, "tach")
    assert rc != 0 and "brand_new" in out, out


def test_git_scan_flags_knowledge_running_git(tree):
    """knowledge.py may import subprocess (import-linter allows it), so a git call hidden there is
    caught by the AST scan instead."""
    _offend(tree, "knowledge", 'import subprocess\nsubprocess.run(["git", "status"])')
    problems = ac.scan_git_usage(str(tree))
    assert any(p.startswith("knowledge.py") for p in problems), problems


@pytest.mark.parametrize("code", [
    'import subprocess\nsubprocess.call("git commit -am x", shell=True)',
    'import os\nos.system("ls")',
    'CMD = ["git", "-C", "x", "log"]',
])
def test_git_scan_flags_variants(tree, code):
    _offend(tree, "add_fact", code)
    assert ac.scan_git_usage(str(tree)), code


def test_git_scan_allows_private_git(tree):
    _offend(tree, "private_git", 'import subprocess\nsubprocess.run(["git", "status"])')
    assert ac.scan_git_usage(str(tree)) == []


@pytest.mark.parametrize("code", [
    'import private_git\nprivate_git.ensure_clean_tree("repo")',
    'from private_git import commit_private_change as commit\ncommit([], "message", "repo")',
    'import private_git as pg\npg.describe_repo("repo")',
])
def test_git_scan_requires_the_private_git_port(tree, code):
    _offend(tree, "cli_entities", code)
    problems = ac.scan_git_usage(str(tree))
    assert any("direct private_git call" in p for p in problems), problems


def test_shadow_rewrites_lazy_and_aliased_imports(tmp_path):
    """The shadow package must see function-level, aliased, multi-name and from-imports."""
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    out.mkdir()
    (src / "a.py").write_text("import os, b as bee\nfrom b import x\ndef f():\n    import c\n    from c import y\n")
    (src / "b.py").write_text("")
    (src / "c.py").write_text("")
    ac.make_shadow(str(src), str(out))
    text = (out / "kn" / "a.py").read_text()
    assert "import os, kn.b as bee" in text and "from kn.b import x" in text
    assert "import kn.c" in text and "from kn.c import y" in text


def test_unused_allowlist_entry_is_reported_by_tach(tree):
    """`exact = true`: a depends_on entry that is not imported any more fails, so the allowlists
    (including the `paths` one) cannot silently stay wider than the code."""
    (tree / "add_fact.py").write_text(
        "".join(f"import {d}\n" for d in _tach_modules(_read(TACH_TOML))["add_fact"]["depends_on"] if d != "private_git"))
    rc, out = _check(tree, "tach")
    assert rc != 0 and "private_git" in out, out


# --------------------------------------------------------------------- source scans (tests/arch_check.py)
#
# tach and import-linter judge imports. These judge what a module does, with no exemption list: a module either
# sits in a ring that may do the thing or it does not do it.


def test_inner_rings_import_no_concrete_adapter():
    """Model, domain services and application services import nothing from the infrastructure, composition,
    pipeline, CLI or serving rings (read from tach's layers, so a module added to a ring is covered)."""
    problems = ac.scan_inner_ring_imports(REPO)
    assert problems == [], "\n".join(problems)


def test_handles_sql_and_dynamic_loading_stay_in_composition_and_infrastructure():
    """sqlite3, importlib/__import__/spec_from_file_location/exec, sys.path edits and `.execute()`-style calls
    appear only where the ring allows: composition and infrastructure may; the pipeline runs SQL on the
    connection it is handed and may name sqlite3.Row and the sqlite3 errors, never connect or load code."""
    problems = ac.scan_handles_and_dynamic_loading(REPO)
    assert problems == [], "\n".join(problems)


def test_ports_expose_no_database_calls():
    """No `*_port` protocol has an execute/cursor/commit/fetch-style method, imports sqlite3, or (outside the
    build ring's own ports) takes a connection or cursor parameter."""
    problems = ac.scan_port_surface(REPO)
    assert problems == [], "\n".join(problems)


def test_build_ring_ports_are_exactly_the_four_that_take_the_builds_connection():
    """The only ports allowed a connection parameter are pinned: they serve the build and its steps."""
    assert ac.BUILD_RING_PORTS == {"build_files_port", "datafiles_port", "inputs_port", "step_stores_port"}
    for name in ac.BUILD_RING_PORTS:
        assert _tach_modules(_read(TACH_TOML))[name]["layer"] == "domain_services"
    # nothing an application service, the CLI or the inbox depends on is among them
    mods = _tach_modules(_read(TACH_TOML))
    for name, m in mods.items():
        if m["layer"] in ("application", "cli", "serving"):
            assert not set(m.get("depends_on", [])) & ac.BUILD_RING_PORTS, f"{name} depends on a build-ring port"


@pytest.mark.parametrize("module, code, scan, needle", [
    ("privacy", "import privacy_store", "scan_inner_ring_imports", "infrastructure module 'privacy_store'"),
    ("review_service", "def f():\n    import review", "scan_inner_ring_imports", "composition module 'review'"),
    ("fact_rules", "import knowledge", "scan_inner_ring_imports", "cli module 'knowledge'"),
    ("review_service", "import sqlite3", "scan_handles_and_dynamic_loading", "imports sqlite3"),
    ("knowledge", "def f(db):\n    return db.execute('SELECT 1')", "scan_handles_and_dynamic_loading", ".execute()"),
    ("privacy", "def f(c):\n    c.cursor()", "scan_handles_and_dynamic_loading", ".cursor()"),
    ("inbox", "import importlib", "scan_handles_and_dynamic_loading", "dynamic loading"),
    ("build", "import importlib.util\nspec = importlib.util.spec_from_file_location('x', 'y')",
     "scan_handles_and_dynamic_loading", "spec_from_file_location"),
    ("concerts", "import sys\nsys.path.insert(0, '.')", "scan_handles_and_dynamic_loading", "sys.path"),
    ("music_ratings", "import sqlite3\nc = sqlite3.connect('x')", "scan_handles_and_dynamic_loading", "sqlite3.connect"),
    ("cli_lifecycle", "x = __import__('os')", "scan_handles_and_dynamic_loading", "__import__"),
    ("facts_batch_service", "exec('1')", "scan_handles_and_dynamic_loading", "exec()"),
    ("clock_port", "class P:\n    def execute(self, sql): ...", "scan_port_surface", "execute()"),
    ("ids_port", "class P:\n    def lookup(self, con): ...", "scan_port_surface", "database handle (con)"),
    ("revisions_port", "import sqlite3", "scan_port_surface", "imports sqlite3"),
])
def test_scans_report_what_they_are_for(tree, module, code, scan, needle):
    """Each scan can fail: an offending line in a synthetic tree (real tach.toml, empty modules) is named."""
    _offend(tree, module, code)
    problems = getattr(ac, scan)(str(tree))
    assert any(needle in p and p.startswith(module + ".py") for p in problems), problems


@pytest.mark.parametrize("module, code", [
    ("music_ratings", "import sqlite3\ndef f(con):\n    try:\n        con.execute('x')\n    except sqlite3.IntegrityError:\n        pass"),
    ("measurements", "import sqlite3\ndef f(con):\n    con.row_factory = sqlite3.Row"),
    ("local_buildfiles", "import importlib.util, sqlite3\nsqlite3.connect('x')"),
    ("paths", "import importlib.util\nimportlib.util.spec_from_file_location('x', 'y')"),
    ("container", "import sqlite3"),
    ("step_stores_port", "class P:\n    def write(self, con, rows): ..."),
    ("fact_rules", "import re\nX = re.compile('a')"),
    ("scripts_service", "def f(deps):\n    deps.runner.run_module('build')"),
])
def test_scans_leave_what_a_ring_may_do_alone(tree, module, code):
    _offend(tree, module, code)
    for scan in (ac.scan_inner_ring_imports, ac.scan_handles_and_dynamic_loading, ac.scan_port_surface):
        assert [p for p in scan(str(tree)) if p.startswith(module + ".py")] == [], (module, scan.__name__)


# --------------------------------------------------------------------- wiring covers every composed port


def _port_protocols():
    """{module: [Protocol class names]} for every `*_port` module."""
    import ast
    out = {}
    for name, path in sorted(ac._source_files(REPO).items()):
        if not name.endswith("_port"):
            continue
        tree = ast.parse(_read(path))
        out[name] = [n.name for n in tree.body if isinstance(n, ast.ClassDef)
                     and any(ast.unparse(b).split(".")[-1] == "Protocol" for b in n.bases)]
    return out


# Ports whose adapter is a composition root's bound API (a SimpleNamespace made at runtime), so mypy has no module
# to assign: wiring.verify checks them against the composed API at every compose(). Everything else is also
# assigned in tests/port_types.py.
FACADE_PORTS = {"AddFact", "Reviews"}


def test_every_port_protocol_is_composed_through_wiring_or_is_a_build_ring_port():
    """A protocol that no `*Deps` can name and wiring.verify does not know is a port nothing checks."""
    import wiring
    build_ring = {"BuildFiles", "DataFiles", "Inputs", "StepStores"}
    declared = {cls for classes in _port_protocols().values() for cls in classes}
    verified = {p.__name__ for p in wiring.PORT_PROTOCOLS.values()}
    assert declared - verified == build_ring, f"unverified ports: {sorted(declared - verified - build_ring)}"
    assert not verified - declared, f"wiring.PORT_PROTOCOLS names a protocol no *_port module declares: {sorted(verified - declared)}"


def test_every_deps_field_is_a_known_port_typed_as_its_protocol():
    """Each service's `*Deps` fields are `locations` or a name in wiring.PORT_PROTOCOLS annotated with exactly that
    protocol, so wiring.verify checks every dependency a service can be given."""
    import dataclasses
    import importlib
    import wiring
    from locations import Locations
    seen = 0
    for name in sorted(n for n in ac.importable_stems(REPO) if n.endswith("_service")):
        mod = importlib.import_module(name)
        for attr, cls in vars(mod).items():
            if attr.endswith("Deps") and dataclasses.is_dataclass(cls) and cls.__module__ == name:
                seen += 1
                for f in dataclasses.fields(cls):
                    if f.name == "locations":
                        assert f.type in (Locations, "Locations"), (name, f.name)
                        continue
                    assert f.name in wiring.PORT_PROTOCOLS, f"{name}.{attr}.{f.name} is not in wiring.PORT_PROTOCOLS"
                    assert f.type is wiring.PORT_PROTOCOLS[f.name], f"{name}.{attr}.{f.name} is typed {f.type}"
    assert seen >= len([n for n in ac.importable_stems(REPO) if n.endswith("_service")]), "a service has no *Deps class"


def test_every_composition_root_verifies_before_it_publishes():
    """Every `compose()` builds its `*Deps` and returns `bind_all(...)` (which calls wiring.verify), and nothing in a
    composition root constructs a `*Deps` outside such a function."""
    import ast
    roles = _architecture_roles()
    roots = sorted(n for n, r in roles.items() if r == "composition root" and n != "container")
    assert roots
    for name in roots:
        tree = ast.parse(_read(os.path.join(REPO, name + ".py")))
        composes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "compose"]
        assert len(composes) == 1, f"{name}.py needs exactly one compose()"
        compose = composes[0]
        calls = {ast.unparse(c.func).split(".")[-1] for c in ast.walk(compose) if isinstance(c, ast.Call)}
        assert calls & {"bind_all", "verify"}, f"{name}.compose() never reaches wiring.verify"
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func).split(".")[-1].endswith("Deps"):
                assert any(node is c for c in ast.walk(compose)), f"{name}.py:{node.lineno}: a Deps built outside compose()"


def test_the_container_composes_every_root_and_returns_one_with_verified_ports():
    """The Container hands out each composition root once through compose(): none is missing, so no root exists whose
    ports nobody verifies."""
    import ast
    roles = _architecture_roles()
    roots = {n for n, r in roles.items() if r == "composition root" and n != "container"}
    tree = ast.parse(_read(os.path.join(REPO, "container.py")))
    used = {ast.unparse(c.func).split(".")[0] for c in ast.walk(tree) if isinstance(c, ast.Call)
            and ast.unparse(c.func).endswith(".compose")}
    assert used == roots, f"container does not compose: {sorted(roots - used)}; composes a non-root: {sorted(used - roots)}"


def test_build_ring_adapters_conform_to_their_ports(monkeypatch):
    """The four build-ring ports are not composed through a *Deps, so run the same call-compatibility check wiring uses
    on them (member names and parameter order), here, with the real adapters."""
    import wiring
    import build_files_port, datafiles_port, inputs_port, step_stores_port
    import local_buildfiles, local_datafiles, local_inputs, local_stores
    pairs = {"files": (build_files_port.BuildFiles, local_buildfiles), "datafiles": (datafiles_port.DataFiles, local_datafiles),
             "inputs": (inputs_port.Inputs, local_inputs), "stores": (step_stores_port.StepStores, local_stores)}
    for key, (proto, adapter) in pairs.items():
        monkeypatch.setitem(wiring.PORT_PROTOCOLS, key, proto)
        assert wiring.conformance_problems(key, adapter) == [], key


# --------------------------------------------------------------------- mypy covers every port, service and adapter


def test_mypy_covers_every_port_service_and_composition_root_and_assigns_each_adapter():
    """[tool.mypy] files lists every *_port, *_service and composition root plus wiring/container, and
    tests/port_types.py assigns an adapter to each port (the facade-shaped ports are checked at runtime by wiring)."""
    import ast
    files = set(tomllib.loads(_read(PYPROJECT))["tool"]["mypy"]["files"])
    roles = _architecture_roles()
    expected = {f"{n}.py" for n in ac.importable_stems(REPO)
                if n.endswith(("_port", "_service")) or roles.get(n) == "composition root"}
    expected |= {"wiring.py", "tests/port_types.py"}
    assert expected <= files, f"mypy does not check: {sorted(expected - files)}"
    assert all(os.path.exists(os.path.join(REPO, f)) for f in files), sorted(f for f in files if not os.path.exists(os.path.join(REPO, f)))
    annotated = {ast.unparse(n.annotation) for n in ast.walk(ast.parse(_read(os.path.join(REPO, "tests", "port_types.py"))))
                 if isinstance(n, ast.AnnAssign)}
    declared = {cls for classes in _port_protocols().values() for cls in classes}
    assert declared - annotated == FACADE_PORTS, f"ports with no static adapter assignment: {sorted(declared - annotated - FACADE_PORTS)}"


# --------------------------------------------------------------------- the gate has no migration exemptions

FORBIDDEN_LIST_NAME = re.compile(r"(EXEMPT|WAIVE|GRANDFATHER|LEGACY|NOT_YET|STILL_|MIGRAT|TEMPORARY|UNMIGRATED)", re.I)


def test_no_migration_exemption_remains_in_the_gate():
    """Every migration is finished, so the gate lists no module as still allowed to hold a connection, do raw I/O or
    load code by path: no such named collection in the architecture tests or the scan helpers, no module-level
    `*_EXEMPT*`-style name, only the documented `ignore_imports` and the one `tach-ignore`, and the scans above
    take no module list that would excuse a module."""
    import ast
    for path in (os.path.join(REPO, "tests", "test_architecture.py"), os.path.join(REPO, "tests", "arch_check.py")):
        for node in ast.parse(_read(path)).body:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
            for t in targets:
                if isinstance(t, ast.Name):
                    assert not FORBIDDEN_LIST_NAME.search(t.id), f"{os.path.basename(path)}:{node.lineno}: {t.id}"
    import inspect
    scan_params = {n: list(inspect.signature(getattr(ac, n)).parameters)
                   for n in ("scan_inner_ring_imports", "scan_handles_and_dynamic_loading", "scan_port_surface")}
    assert scan_params == {n: ["src_dir"] for n in scan_params}, scan_params
    # the raw-I/O test and the other I/O tests skip exactly the infrastructure ring and nothing else
    text = _read(os.path.join(REPO, "tests", "test_architecture.py"))
    assert 'if layer.get(name) == "infrastructure":\n            continue' in text
    assert "BUILD_TOOLING)" not in text.split("def test_raw_file_and_database_io_lives_only_in_adapters")[1].split("def test_maintenance")[0]
    for module in ("build", "normal_db", "leak_test", "leak_scan_store", "local_buildfiles"):
        assert module in ac.module_layers(REPO)
    # build.py and the build tooling load nothing by path themselves: the one loader is the adapter's
    for name in ("build", "normal_db", "leak_test", "leak_scan_store"):
        assert "importlib" not in _read(os.path.join(REPO, name + ".py")), name


def test_migration_status_table_names_real_modules_and_covers_every_service_and_port():
    """docs/architecture.md's per-subsystem status table must not drift: every module it names exists, and every
    application service and every port appears in it."""
    text = _read(os.path.join(REPO, "docs", "architecture.md"))
    section = text.split("## Migration status", 1)[1].split("\n## ", 1)[0]
    on_disk = set(ac.top_level_stems(REPO))
    named = {tok for row in section.splitlines() if row.startswith("| ") for tok in re.findall(r"`([a-z_]+)`", row)}
    named = {n for n in named if n not in {"verify", "compose"}}
    modules = {n for n in named if n.endswith(("_service", "_port", "_store")) or n in on_disk}
    assert modules <= on_disk | {"wiring"}, f"names no module: {sorted(modules - on_disk)}"
    for kind in ("_service", "_port"):
        missing = {n for n in on_disk if n.endswith(kind)} - named
        assert not missing, f"missing from the migration status table: {sorted(missing)}"
